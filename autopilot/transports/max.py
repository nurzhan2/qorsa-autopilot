"""Транспорт MAX.

Сверено с https://dev.max.ru/docs-api. Отличия от Telegram, которые важны:

* авторизация — HTTP-заголовком `Authorization: <token>`, передача токена
  query-параметром больше не поддерживается; старый `botapi.max.ru` устарел;
* тип события лежит в явном поле `update_type` (`message_created` и другие),
  а не выводится из наличия поля, как в Telegram;
* курсор потока называется `marker`, а не `offset`;
* long polling официально ограничен по скорости и сроку хранения событий и
  годится для локальной работы; для сервера рекомендован webhook
  (`POST /subscriptions`), и одновременно работает только один способ;
* аналога Telegram Business нет: бот пишет **от своего лица**, поэтому
  `supports_impersonation()` = False, а Communicator обязан подписывать
  сообщения как бот.

Хост вынесен в MAX_API_BASE: справочник dev.max.ru отдаёт
`platform-api2.max.ru`, часть публикаций — `platform-api.max.ru`.
Проверь на своём токене и поправь в .env, если не сойдётся.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import AsyncIterator

import httpx

from ..config import cfg
from ..db import utcnow
from .base import Backoff, InboundMessage, load_offset

log = logging.getLogger("max")

# события, которые нас интересуют
UPDATE_TYPES = ["message_created", "message_edited", "message_removed", "bot_started"]

ATTACHMENT_KINDS = {
    "image": "photo", "video": "video", "audio": "audio", "file": "document",
    "sticker": "sticker", "contact": "contact", "location": "location", "share": "share",
}


class MaxTransport:
    name = "max"

    def __init__(self, token: str | None = None, base_url: str | None = None,
                 client: httpx.AsyncClient | None = None, account: str = "qorsa"):
        self.token = token or cfg.max_token
        self.base_url = (base_url or cfg.max_api_base).rstrip("/")
        self._client = client
        self._own_client = client is None
        self.poll_timeout = cfg.max_poll_timeout
        self.account = str(account)

    @property
    def key(self) -> tuple[str, str]:
        return (self.account, self.name)

    def supports_impersonation(self) -> bool:
        # В MAX нет бизнес-режима: что бы мы ни делали, сообщение придёт от бота.
        # Притворяться человеком здесь нельзя — см. Communicator.
        return False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": self.token},
                timeout=httpx.Timeout(self.poll_timeout + 15))
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._own_client:
            await self._client.aclose()
            self._client = None

    # ---------- поллинг ----------

    async def poll(self) -> AsyncIterator[InboundMessage]:
        marker = await load_offset(self.name, self.account)
        backoff = Backoff()

        while True:
            params = {"limit": 100, "timeout": self.poll_timeout,
                      "types": ",".join(UPDATE_TYPES)}
            if marker:
                params["marker"] = marker
            try:
                r = await self.client.get("/updates", params=params)
                if r.status_code == 429:
                    retry = float(r.headers.get("Retry-After", 5))
                    log.warning("429 от MAX, жду %.0fs", retry)
                    await backoff.sleep(forced=retry)
                    continue
                r.raise_for_status()
                data = r.json()
                backoff.reset()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("/updates не удался: %s", e)
                await backoff.sleep()
                continue

            updates = data.get("updates") or []
            for upd in updates:
                msg = self.normalize(upd)
                if msg is not None:
                    yield msg
            # marker двигаем только после того, как ingest подтвердит запись
            new_marker = data.get("marker")
            if new_marker is not None:
                marker = str(new_marker)

            if not updates:
                await asyncio.sleep(0.2)

    def normalize(self, upd: dict) -> InboundMessage | None:
        """MAX объявляет тип события явно — по нему и разбираем."""
        kind = upd.get("update_type")
        if kind == "message_removed":
            return InboundMessage(
                transport=self.name,
                chat_id=str(upd.get("chat_id", "")),
                message_id=str(upd.get("message_id", "")),
                deleted=True,
                cursor=str(upd.get("marker") or ""),
                date=_ts(upd.get("timestamp")),
                raw=upd)

        if kind not in ("message_created", "message_edited"):
            return None

        message = upd.get("message") or {}
        body = message.get("body") or {}
        sender = message.get("sender") or {}
        recipient = message.get("recipient") or {}
        attachments = body.get("attachments") or []
        media_kind = None
        if attachments:
            media_kind = ATTACHMENT_KINDS.get(attachments[0].get("type"), "other")

        chat_id = recipient.get("chat_id")
        if chat_id is None:
            chat_id = recipient.get("user_id")
        link = message.get("link") or {}
        reply_msg = link.get("message") or {}
        reply_sender = str((link.get("sender") or {}).get("user_id") or "") or None
        text = body.get("text") or ""
        mentions = bool(cfg.bot_username and ("@" + cfg.bot_username.lower()) in text.lower())
        if cfg.bot_max_id and reply_sender and reply_sender == str(cfg.bot_max_id):
            mentions = True

        return InboundMessage(
            transport=self.name,
            chat_id=str(chat_id or ""),
            message_id=str(body.get("mid") or ""),
            text=text,
            date=_ts(upd.get("timestamp") or message.get("timestamp")),
            sender_id=str(sender.get("user_id")) if sender.get("user_id") is not None else None,
            handle=("@" + sender["username"]) if sender.get("username") else None,
            has_media=bool(attachments),
            media_kind=media_kind,
            reply_to=str(reply_msg.get("mid")) if link.get("type") == "reply" else None,
            reply_to_sender_id=reply_sender,
            sender_name=(sender.get("name") or sender.get("username") or "").strip(),
            chat_title=recipient.get("chat_title") or upd.get("chat_title") or "",
            chat_type=recipient.get("chat_type") or "dialog",
            mentions_bot=mentions,
            edited=(kind == "message_edited"),
            connection_id=None,          # бизнес-режима в MAX нет
            cursor=str(upd.get("marker") or ""),
            raw=upd,
        )

    # ---------- отправка ----------

    async def send(self, chat_ref: str, text: str, connection_id: str | None = None) -> str:
        """chat_id уходит query-параметром, тело — NewMessageBody.
        connection_id игнорируется: подставлять его некуда и не от кого."""
        params = {"chat_id": chat_ref} if str(chat_ref).lstrip("-").isdigit() else {"user_id": chat_ref}
        r = await self.client.post("/messages", params=params, json={"text": text})
        r.raise_for_status()
        data = r.json()
        return str(((data.get("message") or {}).get("body") or {}).get("mid", ""))

    async def resolve_chat(self, handle: str) -> str | None:
        """Публичного резолва @username у MAX нет — привязка происходит по
        первому входящему сообщению, как и в Telegram."""
        return None

    # ---------- webhook: заготовка для сервера ----------

    async def subscribe_webhook(self, url: str) -> dict:
        r = await self.client.post("/subscriptions", json={"url": url, "update_types": UPDATE_TYPES})
        r.raise_for_status()
        return r.json()

    async def drop_webhook(self, url: str) -> dict:
        r = await self.client.request("DELETE", "/subscriptions", params={"url": url})
        r.raise_for_status()
        return r.json()


def _ts(value) -> dt.datetime:
    """MAX отдаёт timestamp в миллисекундах."""
    if not value:
        return utcnow()
    try:
        v = float(value)
    except (TypeError, ValueError):
        return utcnow()
    if v > 1e11:            # похоже на миллисекунды
        v /= 1000.0
    return dt.datetime.fromtimestamp(v, dt.timezone.utc)


def build_webhook_router(on_message):
    """FastAPI-роут для будущего сервера.

    FastAPI намеренно НЕ в requirements: локально работает long polling, а
    веб-сервер понадобится только когда автопилот переедет на хост.
    Тогда: pip install fastapi uvicorn
    """
    from fastapi import APIRouter, Request        # локальный импорт: см. выше

    router = APIRouter()
    transport = MaxTransport()

    @router.post("/max/webhook")
    async def _hook(request: Request):
        payload = await request.json()
        msg = transport.normalize(payload)
        if msg is not None:
            await on_message(transport, msg)
        return {"ok": True}       # MAX отписывает бота, если не отвечать 200

    return router
