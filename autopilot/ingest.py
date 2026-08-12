"""Приём входящих сообщений из мессенджеров.

Порядок обработки одного сообщения жёсткий, менять его нельзя:

1. **перехват секретов** — до всего остального, чтобы пароль не успел
   попасть ни в БД, ни в лог;
2. **привязка к проекту** — по (transport, chat_id), иначе по @username из
   колонки менеджера;
3. **запись в chat_messages** — с дедупликацией по уникальному индексу;
4. **подтверждение offset** — только теперь, иначе падение между шагами
   теряет сообщение;
5. постановка chat-задачи и передача в `Communicator.incoming`.

Сообщение из непривязанного чата не теряется никогда: оно ложится в базу с
`project_id = NULL`, а владельцу уходит уведомление со списком открытых
проектов.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import roles
from .config import cfg
from .db import ChatMessage, Project, ProjectChat, Session, Task, utcnow
from .groups import match_project
from .secrets_scan import scrub
from .transports.base import Backoff, InboundMessage, save_offset

log = logging.getLogger("ingest")

BIND_HINT = "/bind"

GROUP_TYPES = ("group", "supergroup", "chat")


def is_group(msg: InboundMessage) -> bool:
    return (msg.chat_type or "").lower() in GROUP_TYPES


def parse_chat_ref(raw: str) -> tuple[str, str] | None:
    """`tg:@ivan` / `max:@ivan` / `@ivan` / `12345` -> (transport, handle).

    Без префикса — telegram: колонка «Чат клиента» выросла из «TG chat»,
    и уже заполненные строки ломать нельзя.
    """
    value = (raw or "").strip()
    if not value:
        return None
    transport = "telegram"
    if ":" in value:
        prefix, rest = value.split(":", 1)
        prefix = prefix.strip().lower()
        if prefix in ("tg", "telegram"):
            transport, value = "telegram", rest.strip()
        elif prefix == "max":
            transport, value = "max", rest.strip()
    return (transport, value) if value else None


class Ingest:
    def __init__(self, transports, communicator, scheduler=None):
        self.transports = {t.name: t for t in transports}
        self.communicator = communicator
        self.scheduler = scheduler
        # последний неопознанный чат — чтобы «/bind 7» без аргументов сработал
        self._last_unbound: tuple[str, str] | None = None

    # ---------- запуск ----------

    async def run(self) -> None:
        if not roles.owner_configured():
            # Без id владельца система примет собственные реплики за слова
            # клиента и потащит их в ТЗ как требования. Это хуже, чем не
            # работать вовсе, поэтому падаем сразу и громко.
            log.critical("OWNER_TG_ID (или OWNER_MAX_ID) не задан — ingest не запускается: "
                         "без него владелец неотличим от клиента")
            raise RuntimeError("OWNER_TG_ID не задан")
        if not self.transports:
            log.warning("ни один транспорт не настроен — ingest не поднимается")
            return
        await asyncio.gather(*(self._loop(t) for t in self.transports.values()))

    async def _loop(self, transport) -> None:
        backoff = Backoff()
        while True:
            try:
                log.info("поллер %s запущен", transport.name)
                async for msg in transport.poll():
                    await self.handle(transport, msg)
                    backoff.reset()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("поллер %s упал, переподключаюсь", transport.name)
                await backoff.sleep()

    # ---------- обработка одного сообщения ----------

    async def handle(self, transport, msg: InboundMessage) -> ChatMessage | None:
        try:
            if msg.deleted:
                row = await self._mark_deleted(msg)
            elif msg.edited:
                row = await self._apply_edit(msg)
            else:
                row = await self._store_new(transport, msg)
        finally:
            # offset подтверждаем в любом случае: повторная обработка того же
            # апдейта безопасна (дедуп по индексу), а вечный цикл на ядовитом
            # сообщении — нет
            if msg.cursor:
                await save_offset(transport.name, msg.cursor)
        return row

    async def _store_new(self, transport, msg: InboundMessage) -> ChatMessage:
        project_id, is_new_binding = await self._bind(transport, msg)

        # ШАГ 1: секреты вырезаются ДО записи в БД
        text, secret_names = scrub(msg.text, project_id=project_id, chat_id=msg.chat_id)

        # роль решает, попадут ли слова в бриф: реплики менеджера в ТЗ не идут
        sender_role = await roles.remember(msg.transport, msg.chat_id, msg.sender_id,
                                           msg.sender_name)

        row = ChatMessage(
            transport=msg.transport, chat_id=msg.chat_id, tg_message_id=msg.message_id,
            project_id=project_id, direction=msg.direction, sender_id=msg.sender_id,
            sender_role=sender_role,
            text=text, has_media=msg.has_media, media_kind=msg.media_kind,
            reply_to=msg.reply_to, raw_json=_safe_raw(msg, text),
            created_at=msg.date or utcnow(),
        )
        async with Session() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                # дубль при переподключении — норма, а не ошибка
                await s.rollback()
                log.debug("дубль %s/%s/%s пропущен",
                          msg.transport, msg.chat_id, msg.message_id)
                return await self._existing(msg)

        if await self._maybe_bind_command(msg):
            return row

        if project_id is None:
            await self._report_unbound(msg)
            return row

        if sender_role in (roles.BOT, roles.OWNER, roles.MANAGER):
            # своих в базу пишем (бриф читает всю переписку), но реагировать
            # на них не надо: это не обращение к боту
            return row

        # ВНИМАНИЕ: дальше идёт только `text` — вычищенный. Сырой msg.text
        # ниже этой строки использовать нельзя, иначе пароль осядет в tasks.prompt
        await self._on_client_reply(project_id, transport, msg, text, secret_names)
        return row

    async def _existing(self, msg: InboundMessage) -> ChatMessage | None:
        async with Session() as s:
            return (await s.execute(
                select(ChatMessage).where(
                    ChatMessage.transport == msg.transport,
                    ChatMessage.chat_id == msg.chat_id,
                    ChatMessage.tg_message_id == msg.message_id))).scalars().first()

    async def _apply_edit(self, msg: InboundMessage) -> ChatMessage | None:
        """Правку отражаем, историю не затираем: старый текст уходит в raw_json."""
        text, _ = scrub(msg.text, chat_id=msg.chat_id)
        async with Session() as s:
            row = (await s.execute(
                select(ChatMessage).where(
                    ChatMessage.transport == msg.transport,
                    ChatMessage.chat_id == msg.chat_id,
                    ChatMessage.tg_message_id == msg.message_id))).scalars().first()
            if row is None:
                return None
            history = dict(row.raw_json or {})
            history.setdefault("revisions", []).append(
                {"text": row.text, "at": utcnow().isoformat()})
            row.raw_json = history
            row.text = text
            row.edited_at = utcnow()
            await s.commit()
            return row

    async def _mark_deleted(self, msg: InboundMessage) -> ChatMessage | None:
        async with Session() as s:
            row = (await s.execute(
                select(ChatMessage).where(
                    ChatMessage.transport == msg.transport,
                    ChatMessage.chat_id == msg.chat_id,
                    ChatMessage.tg_message_id == msg.message_id))).scalars().first()
            if row is None:
                return None
            row.deleted = True          # текст оставляем: удаление у клиента
            row.edited_at = utcnow()    # не должно стирать нашу историю
            await s.commit()
            return row

    # ---------- привязка чата к проекту ----------

    async def _bind(self, transport, msg: InboundMessage) -> tuple[int | None, bool]:
        async with Session() as s:
            chat = (await s.execute(
                select(ProjectChat).where(
                    ProjectChat.transport == msg.transport,
                    ProjectChat.chat_id == msg.chat_id))).scalars().first()
            if chat is not None:
                stored, project_id = chat.handle, chat.project_id
        if chat is not None:
            # Привязка живёт по chat_id и переименование её не рвёт.
            # Название всего лишь обновляем, а расхождение с таблицей
            # показываем предупреждением — это повод посмотреть, а не повод
            # потерять чат
            if msg.chat_title and msg.chat_title != stored:
                await self._rename_chat(msg, stored, project_id)
            return project_id, False

        # групповой чат опознаём по названию: менеджер называет их по шаблону
        if msg.chat_title:
            project_id, score, why = await match_project(msg.chat_title)
            if project_id is not None:
                await self.bind_chat(project_id, msg.transport, msg.chat_id,
                                     handle=msg.chat_title, group=is_group(msg))
                log.info("группа %r привязана к проекту %s: %s",
                         msg.chat_title, project_id, why)
                return project_id, True
            log.info("группу %r не опознал: %s", msg.chat_title, why)

        async with Session() as s:
            if not msg.handle:
                return None, False

            # менеджер написал @username, chat_id он не знает — резолвим при
            # первом же сообщении и запоминаем сами
            wanted = msg.handle.lower().lstrip("@")
            candidates = (await s.execute(
                select(Project).where(Project.chat_ref.isnot(None)))).scalars().all()
            for p in candidates:
                parsed = parse_chat_ref(p.chat_ref)
                if not parsed:
                    continue
                tname, handle = parsed
                if tname != msg.transport:
                    continue
                if handle.lower().lstrip("@") != wanted:
                    continue
                s.add(ProjectChat(project_id=p.id, transport=msg.transport,
                                  chat_id=msg.chat_id, handle=msg.handle, is_primary=True))
                await s.commit()
                log.info("чат %s:%s привязан к проекту %s по %s",
                         msg.transport, msg.chat_id, p.id, msg.handle)
                return p.id, True
        return None, False

    async def _rename_chat(self, msg: InboundMessage, old: str | None,
                           project_id: int) -> None:
        """Группу переименовали. Привязку не трогаем, название обновляем."""
        async with Session() as s:
            row = (await s.execute(
                select(ProjectChat).where(ProjectChat.transport == msg.transport,
                                          ProjectChat.chat_id == msg.chat_id))).scalars().first()
            if row is not None:
                row.handle = msg.chat_title
                await s.commit()
        log.info("группа %s переименована: %r -> %r (проект %s, привязка сохранена)",
                 msg.chat_id, old or "—", msg.chat_title, project_id)

        matched, score, why = await match_project(msg.chat_title)
        if matched is not None and matched != project_id:
            await self._warn_owner(
                f"Группа {msg.transport}:{msg.chat_id} переименована в "
                f"«{msg.chat_title}» и теперь похожа на другой проект ({why}). "
                f"Чат остаётся привязан к проекту {project_id}. "
                f"Если это ошибка — «{BIND_HINT} <id> {msg.chat_id}».")
        elif matched is None and score < cfg.group_match_threshold:
            await self._warn_owner(
                f"Группа {msg.transport}:{msg.chat_id} называется «{msg.chat_title}», "
                f"это не похоже ни на один проект ({why}). Привязка к проекту "
                f"{project_id} сохранена, но название стоит поправить.")

    async def _warn_owner(self, text: str) -> None:
        log.warning(text)
        notify = getattr(self.communicator, "notify_owner", None)
        if notify is not None:
            await notify(text)

    async def bind_chat(self, project_id: int, transport: str, chat_id: str,
                        handle: str | None = None, group: bool = False) -> bool:
        """Ручная привязка (ответ владельца или scripts/bind_chat.py)."""
        async with Session() as s:
            if await s.get(Project, project_id) is None:
                return False
            exists = (await s.execute(
                select(ProjectChat).where(ProjectChat.transport == transport,
                                          ProjectChat.chat_id == chat_id))).scalars().first()
            if exists is None:
                s.add(ProjectChat(project_id=project_id, transport=transport,
                                  chat_id=chat_id, handle=handle, is_primary=True,
                                  is_group=group))
            else:
                exists.project_id = project_id
                if handle:
                    exists.handle = handle
                if group:
                    exists.is_group = True
            # осиротевшие сообщения этого чата тоже получают проект
            for row in (await s.execute(
                    select(ChatMessage).where(ChatMessage.transport == transport,
                                              ChatMessage.chat_id == chat_id,
                                              ChatMessage.project_id.is_(None)))).scalars():
                row.project_id = project_id
            await s.commit()
        log.info("чат %s:%s привязан к проекту %s вручную", transport, chat_id, project_id)
        return True

    async def _maybe_bind_command(self, msg: InboundMessage) -> bool:
        """`/bind 7` от владельца привязывает последний неопознанный чат."""
        text = (msg.text or "").strip()
        if not text.startswith(BIND_HINT) or str(msg.chat_id) != str(cfg.tg_owner):
            return False
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return False
        project_id = int(parts[1])
        target = (msg.transport, parts[2]) if len(parts) > 2 else self._last_unbound
        if target is None:
            return False
        ok = await self.bind_chat(project_id, target[0], target[1])
        if ok:
            self._last_unbound = None
        return ok

    async def _report_unbound(self, msg: InboundMessage) -> None:
        """Сообщение сохранено, но чей оно — неизвестно. Спрашиваем владельца."""
        self._last_unbound = (msg.transport, msg.chat_id)
        async with Session() as s:
            projects = (await s.execute(
                select(Project).where(Project.status.notin_(("done", "blocked"))))).scalars().all()
        lines = "\n".join(f"  {p.id} — {p.title}" for p in projects) or "  (открытых проектов нет)"
        text = (f"Неопознанный чат {msg.transport}:{msg.chat_id}"
                f"{' (' + msg.handle + ')' if msg.handle else ''}. Сообщение сохранено.\n"
                f"Ответь «{BIND_HINT} <id проекта>», чтобы привязать.\n"
                f"Открытые проекты:\n{lines}")
        notify = getattr(self.communicator, "notify_owner", None)
        if notify is not None:
            await notify(text)
        else:
            log.warning(text)

    # ---------- реакция на ответ клиента ----------

    async def _on_client_reply(self, project_id: int, transport, msg: InboundMessage,
                               text: str, secret_names: list[str]) -> None:
        async with Session() as s:
            project = await s.get(Project, project_id)
            if project is None:
                return
            project.client_replied_at = utcnow()   # фаза 4 снимет по этому блокировки
            project.updated_at = utcnow()
            # слот в полосе chat ставим, только если к боту обратились:
            # в группе большинство сообщений — разговор менеджера с клиентом
            if msg.mentions_bot or not is_group(msg):
                s.add(Task(project_id=project_id, lane="chat",
                           order_idx=int(utcnow().timestamp()),
                           title=f"ответ клиенту ({msg.transport})",
                           prompt=(text or "")[:2000], status="ready"))
            await s.commit()
            project = await s.get(Project, project_id)

        if secret_names:
            # про перехват клиенту не сообщаем — просто подтверждаем получение
            await self.communicator.confirm_access_received(project, transport, msg.chat_id)
            return

        if is_group(msg) and not msg.mentions_bot:
            # В группе сидит менеджер. Бот читает всё, но в разговор не влезает:
            # отвечает, только когда обратились к нему. Молчание здесь — это фича
            log.debug("группа %s: сообщение прочитано, ответ не требуется", msg.chat_id)
            return

        await self.communicator.incoming(project, text,
                                         transport=msg.transport, chat_id=msg.chat_id,
                                         in_group=is_group(msg))


def _safe_raw(msg: InboundMessage, scrubbed: str) -> dict:
    """raw_json хранит исходный апдейт, но с уже вырезанным секретом —
    иначе пароль осел бы в базе именно здесь."""
    raw = json.loads(json.dumps(msg.raw, ensure_ascii=False, default=str)) if msg.raw else {}
    for key in ("text", "caption"):
        if isinstance(raw.get(key), str):
            raw[key] = scrubbed
    body = raw.get("body")
    if isinstance(body, dict) and isinstance(body.get("text"), str):
        body["text"] = scrubbed
    message = raw.get("message")
    if isinstance(message, dict) and isinstance(message.get("body"), dict):
        if isinstance(message["body"].get("text"), str):
            message["body"]["text"] = scrubbed
    return raw
