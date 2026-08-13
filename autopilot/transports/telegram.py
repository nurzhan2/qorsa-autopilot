"""Транспорт Telegram через Bot API + Business API.

Требования к аккаунту (проверено по документации, см. CLAUDE.md):
* у владельца бизнес-аккаунта должен быть **Telegram Premium** — без него
  раздела Telegram Business в настройках просто нет;
* у бота в @BotFather должен быть включён **Business Mode**;
* владелец подключает бота в Настройки → Telegram Business → Чат-боты;
* бизнес-апдейты **не приходят по умолчанию** — их обязательно перечислять
  в `allowed_updates`.

Никакого MTProto и userbot: только Bot API.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import AsyncIterator

import httpx

from ..config import cfg
from ..db import BusinessConnection, Session, utcnow
from .base import Backoff, InboundMessage, load_offset

log = logging.getLogger("tg")

# Бизнес-апдейты Telegram не присылает, если их не попросить явно
# Групповые чаты — основной канал; my_chat_member нужен, чтобы узнать
# о добавлении бота в группу и сразу попытаться опознать проект.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "my_chat_member",
]
# Бизнес-режим (личка от лица владельца) выключен по умолчанию: работа
# переехала в группы. Код оставлен на случай возврата — см. CLAUDE.md
BUSINESS_UPDATES = [
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]


def allowed_updates() -> list[str]:
    return ALLOWED_UPDATES + (BUSINESS_UPDATES if cfg.tg_business_enabled else [])

MEDIA_FIELDS = [
    ("photo", "photo"), ("video", "video"), ("document", "document"),
    ("voice", "voice"), ("audio", "audio"), ("sticker", "sticker"),
    ("video_note", "video_note"), ("animation", "animation"),
    ("location", "location"), ("contact", "contact"),
]


def _media_of(msg: dict) -> tuple[bool, str | None]:
    """Медиа не качаем — фиксируем факт и тип."""
    for field, kind in MEDIA_FIELDS:
        if msg.get(field):
            return True, kind
    return False, None


def _text_of(msg: dict) -> str:
    return msg.get("text") or msg.get("caption") or ""


def _mentions_bot(text: str, reply_to_sender_id: str | None) -> bool:
    """Обращение — это упоминание @имени или ответ на сообщение бота."""
    if cfg.bot_tg_id and reply_to_sender_id and str(reply_to_sender_id) == str(cfg.bot_tg_id):
        return True
    if cfg.bot_username and ("@" + cfg.bot_username.lower()) in (text or "").lower():
        return True
    return False


class TelegramTransport:
    name = "telegram"

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None,
                 account: str = "qorsa"):
        self.token = token or cfg.tg_token
        self._client = client
        self._own_client = client is None
        self.poll_timeout = cfg.tg_poll_timeout
        # компания, чей это бот. Нужна для offset и для того, чтобы ingest
        # знал, в чьих чатах он сейчас работает: роль владельца зависит от неё
        self.account = str(account)

    @property
    def key(self) -> tuple[str, str]:
        """Пара, которой транспорт опознаётся однозначно. Одного имени мало:
        «telegram» теперь два, по одному на компанию."""
        return (self.account, self.name)

    def supports_impersonation(self) -> bool:
        # ради этого всё и затевалось: клиенту пишем от лица владельца
        return True

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"https://api.telegram.org/bot{self.token}",
                timeout=httpx.Timeout(self.poll_timeout + 15))
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._own_client:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, **params) -> dict:
        r = await self.client.post(f"/{method}", json=params)
        if r.status_code == 429:
            retry = (r.json().get("parameters") or {}).get("retry_after", 5)
            raise RateLimited(float(retry))
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data.get("result")

    # ---------- поллинг ----------

    async def poll(self) -> AsyncIterator[InboundMessage]:
        """Бесконечный поток сообщений. Сам переживает обрывы связи:
        наверх исключения не пускаем, иначе ingest придётся оборачивать
        тем же самым циклом переподключения."""
        offset = await load_offset(self.name, self.account)
        next_offset = int(offset) + 1 if offset else None
        backoff = Backoff()

        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=next_offset,
                    timeout=self.poll_timeout,
                    allowed_updates=allowed_updates(),
                )
                backoff.reset()
            except RateLimited as e:
                log.warning("429 от Telegram, жду %.0fs", e.retry_after)
                await backoff.sleep(forced=e.retry_after)   # уважаем retry_after как есть
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("getUpdates не удался: %s", e)
                await backoff.sleep()
                continue

            for upd in updates or []:
                # offset сдвигаем ТОЛЬКО через cursor, который подтвердит ingest
                # после записи в БД. Иначе падение между fetch и записью теряет
                # сообщение навсегда
                next_offset = int(upd["update_id"]) + 1
                async for msg in self._explode(upd):
                    yield msg

            if not updates:
                await asyncio.sleep(0.2)

    async def _explode(self, upd: dict) -> AsyncIterator[InboundMessage]:
        cursor = str(upd["update_id"])

        if "business_connection" in upd:
            await self._remember_connection(upd["business_connection"])
            return

        if "deleted_business_messages" in upd:
            d = upd["deleted_business_messages"]
            chat_id = str((d.get("chat") or {}).get("id", ""))
            for mid in d.get("message_ids") or []:
                yield InboundMessage(
                    transport=self.name, chat_id=chat_id, message_id=str(mid),
                    deleted=True, cursor=cursor,
                    connection_id=d.get("business_connection_id"), raw=d)
            return

        for key, edited in (("business_message", False), ("edited_business_message", True),
                            ("message", False), ("edited_message", True)):
            msg = upd.get(key)
            if msg:
                yield self._normalize(msg, cursor=cursor, edited=edited)
                return

    def _normalize(self, msg: dict, *, cursor: str, edited: bool) -> InboundMessage:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        has_media, kind = _media_of(msg)
        ts = msg.get("edit_date") or msg.get("date")
        reply = msg.get("reply_to_message") or {}
        reply_sender = str((reply.get("from") or {}).get("id") or "") or None
        return InboundMessage(
            transport=self.name,
            chat_id=str(chat.get("id", "")),
            message_id=str(msg.get("message_id", "")),
            text=_text_of(msg),
            date=dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else utcnow(),
            sender_id=str(sender.get("id")) if sender.get("id") is not None else None,
            handle=("@" + sender["username"]) if sender.get("username") else None,
            has_media=has_media,
            media_kind=kind,
            reply_to=str(reply.get("message_id")) if reply else None,
            reply_to_sender_id=reply_sender,
            sender_name=(sender.get("first_name") or sender.get("username") or "").strip(),
            chat_title=chat.get("title") or "",
            chat_type=chat.get("type") or "private",
            mentions_bot=_mentions_bot(_text_of(msg), reply_sender),
            edited=edited,
            connection_id=msg.get("business_connection_id"),
            cursor=cursor,
            raw=msg,
        )

    async def _remember_connection(self, conn: dict) -> None:
        """`can_reply` объявлен устаревшим в Bot API 9.0 в пользу объекта
        `rights`. Поддерживаем оба, чтобы не сломаться ни на старом, ни на новом."""
        rights = conn.get("rights") or {}
        can_reply = bool(rights.get("can_reply", conn.get("can_reply", True)))
        async with Session() as s:
            row = await s.get(BusinessConnection, str(conn["id"]))
            if row is None:
                row = BusinessConnection(id=str(conn["id"]), transport=self.name)
                s.add(row)
            row.user_chat_id = str(conn.get("user_chat_id") or "") or None
            row.is_enabled = bool(conn.get("is_enabled", True))
            row.can_reply = can_reply
            row.updated_at = utcnow()
            await s.commit()
        log.info("бизнес-соединение %s: enabled=%s can_reply=%s",
                 conn.get("id"), conn.get("is_enabled"), can_reply)

    # ---------- отправка ----------

    async def send(self, chat_ref: str, text: str, connection_id: str | None = None) -> str:
        params = {"chat_id": chat_ref, "text": text}
        if connection_id:
            # именно это делает сообщение «от меня», а не «от бота»
            params["business_connection_id"] = connection_id
        res = await self.call("sendMessage", **params)
        return str((res or {}).get("message_id", ""))

    async def resolve_chat(self, handle: str) -> str | None:
        """Bot API не умеет резолвить @username приватного пользователя, пока
        тот не написал сам. Пробуем getChat (работает для публичных чатов),
        иначе привязка произойдёт по первому входящему сообщению."""
        if not handle:
            return None
        try:
            res = await self.call("getChat", chat_id=handle)
        except Exception as e:
            log.info("не смог резолвить %s через getChat: %s", handle, e)
            return None
        return str(res.get("id")) if res else None


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"429, retry_after={retry_after}")
        self.retry_after = retry_after
