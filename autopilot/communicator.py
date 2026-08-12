"""Маршрутизация сообщений между тремя участниками.

Участников трое, и автопилот замещает только одного из них:

* МЕНЕДЖЕР — берёт заказы, договаривается о цене и сроках. Всё про деньги,
  сроки, объём и претензии принадлежит ему. Бот сюда не лезет и не смягчает.
* ВЛАДЕЛЕЦ — техчасть: доступы, ключи, технические уточнения. Его и замещаем.
* КЛИЕНТ — даёт доступы и отвечает на технические вопросы.

Сеть на `to_manager` намеренно широкая. Ложное срабатывание стоит одного
лишнего уведомления, пропуск стоит обязательства, которого никто не давал.
Поэтому при любом сомнении — `to_manager`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re

from sqlalchemy import select

from .config import cfg
from .db import (AccessItem, BusinessConnection, Message, Project, ProjectChat,
                 Session, Task, utcnow)

log = logging.getLogger("comm")

TO_CLIENT = "to_client_auto"
TO_OWNER = "to_owner"
TO_MANAGER = "to_manager"

# чем правее, тем строже: при склейке текстов побеждает самый правый
ROUTE_ORDER = (TO_CLIENT, TO_OWNER, TO_MANAGER)

# --- всё, что создаёт обязательство или стоит денег: только менеджеру ---
MANAGER_RE = re.compile(
    r"цен[аыуе]|цено|стоимост|стоит|стоить|сколько|почём|почем|прайс|тариф|смет|"
    r"скидк|оплат|предоплат|аванс|рассрочк|счёт|счет|деньг|бюджет|доплат|"
    r"договор|контракт|услови[яй]|подпис|акт |закрывающ|"
    r"срок|дедлайн|успе|когда будет|когда готов|затягива|задержк|опозда|перенос|"
    r"объ[её]м|доработ|дополнительн|переделат|поменя(ть|ем) тз|изменит[ьс] тз|"
    r"расшир|ещ[её] стран|добавить стран|вне тз|сверх тз|"
    r"претенз|жалоб|недовол|некачествен|возврат|вернуть деньг|компенсац|"
    r"штраф|неустойк|расторг|отказ|суд[ья]?\b|юрист|гарант",
    re.IGNORECASE)

# --- техника сломалась или непонятно: владельцу ---
OWNER_RE = re.compile(
    r"не работает|не пускает|не подход|не могу зайти|не открыва|недоступ|"
    r"отказано в доступе|permission denied|connection refused|access denied|"
    r"неверн(ый|ые) (парол|логин|ключ)|парол[ьи] не|логин не|ключ не|"
    r"истёк|истек|просрочен|невалид|битый|повреждён|поврежден|"
    r"\b(40[0-9]|50[0-9])\b|timeout|ошибк|error|traceback|"
    r"не понимаю|непонятно|не разобрал|уточнить у|нужно уточнение",
    re.IGNORECASE)

# --- то, что бот вправе сказать клиенту сам ---
CLIENT_RE = re.compile(
    r"нужен доступ|нужны доступ|не хватает доступ|пришл[иеё]те? доступ|"
    r"дайте доступ|предостав[ьи]те доступ|запрашива[ею] доступ|чтобы начать, нужн|"
    r"доступ получен|доступ прин[яе]т|получил доступ|проверил доступ|всё пришло|"
    r"готово|выложил|превью|посмотр[ие]",
    re.IGNORECASE)

LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")

# Там, где мессенджер не даёт писать от лица владельца, притворяться человеком
# нельзя. Подписываемся явно — один раз в начале сообщения.
BOT_SIGNATURE = "🤖 Бот по техническим вопросам (не человек).\n\n"


def _unparseable(text: str) -> bool:
    """Стикер, голосовое, одни эмодзи или пустота — человеку разбираться."""
    t = (text or "").strip()
    return not t or not LETTER_RE.search(t)


def route(text: str) -> str:
    """Куда уходит сообщение. Порядок проверок — это и есть приоритет."""
    if MANAGER_RE.search(text or ""):
        return TO_MANAGER
    if _unparseable(text):
        return TO_OWNER
    if OWNER_RE.search(text):
        return TO_OWNER
    if CLIENT_RE.search(text):
        return TO_CLIENT
    return TO_MANAGER          # при сомнении — менеджеру


def escalation(text: str) -> str:
    """Маршрут для КУСКА текста, дописываемого к готовому сообщению.

    Отличается от route() тем, что не применяет правило «при сомнении —
    менеджеру»: иначе безобидный заголовок задачи вроде «шаг 2» уводил бы
    к менеджеру весь накопленный отчёт о готовности.
    """
    if MANAGER_RE.search(text or ""):
        return TO_MANAGER
    if OWNER_RE.search(text or ""):
        return TO_OWNER
    return TO_CLIENT


def strictest(*routes: str) -> str:
    """Склеили два текста — маршрут берём самый строгий из них."""
    return max((r for r in routes if r in ROUTE_ORDER), key=ROUTE_ORDER.index, default=TO_MANAGER)


def human_delay() -> dt.timedelta:
    """Мгновенный ответ клиенту выглядит как бот. Ждём как живой человек.
    Внутренние уведомления (владельцу, менеджеру) не задерживаем никогда."""
    return dt.timedelta(seconds=random.randint(40, 900))


def in_quiet_hours(now_local: dt.datetime | None = None) -> bool:
    """Тихие часы — по ЛОКАЛЬНОМУ времени машины: клиент спит по своему поясу.
    Равные границы (QUIET_START == QUIET_END) выключают тишину совсем."""
    if cfg.quiet_start == cfg.quiet_end:
        return False
    h = (now_local or dt.datetime.now()).hour
    if cfg.quiet_start < cfg.quiet_end:
        return cfg.quiet_start <= h < cfg.quiet_end
    return h >= cfg.quiet_start or h < cfg.quiet_end


def internal_recipient(msg_route: str) -> str:
    """Свои живут в Telegram обычным ботом, без бизнес-соединения."""
    if msg_route == TO_OWNER:
        return cfg.tg_owner or "OWNER"
    return cfg.tg_manager or "MANAGER"


class Communicator:
    def __init__(self, send_fn=None, transports=None):
        # transports задан — шлём по-настоящему; нет — остаётся заглушка фазы 1,
        # на которой держатся DRY_RUN и тесты
        self.transports = {t.name: t for t in (transports or [])}
        self.send_fn = send_fn or self._stub_send

    async def _stub_send(self, chat_id: str, text: str) -> None:
        log.info("[TG -> %s] %s", chat_id, text)

    # ---------- адресация ----------

    async def client_chat(self, project: Project,
                          prefer: str | None = None) -> tuple[str | None, str | None]:
        """Куда писать клиенту: в тот мессенджер, откуда пришёл вопрос,
        иначе в основной чат проекта."""
        async with Session() as s:
            chats = (await s.execute(
                select(ProjectChat).where(ProjectChat.project_id == project.id)
                .order_by(ProjectChat.is_primary.desc(), ProjectChat.id))).scalars().all()
        if not chats:
            return None, None
        if prefer:
            for c in chats:
                if c.transport == prefer:
                    return c.transport, c.chat_id
        return chats[0].transport, chats[0].chat_id

    async def _connection_id(self, transport_name: str) -> str | None:
        """business_connection_id — то, что превращает «бот написал»
        в «владелец написал»."""
        async with Session() as s:
            row = (await s.execute(
                select(BusinessConnection)
                .where(BusinessConnection.transport == transport_name,
                       BusinessConnection.is_enabled.is_(True),
                       BusinessConnection.can_reply.is_(True))
                .order_by(BusinessConnection.updated_at.desc()))).scalars().first()
        return row.id if row else None

    async def notify_owner(self, text: str) -> None:
        """Оперативное уведомление владельцу — мимо очереди и тихих часов."""
        transport = self.transports.get("telegram")
        chat = cfg.tg_owner or "OWNER"
        if transport is None:
            await self.send_fn(chat, text)
            return
        try:
            await transport.send(chat, text)
        except Exception:
            log.exception("не смог уведомить владельца")

    # ---------- создание ----------

    async def draft(self, project: Project, text: str, kind: str = "plain",
                    force_route: str | None = None,
                    transport: str | None = None, chat_id: str | None = None) -> Message:
        msg_route = strictest(route(text), force_route) if force_route else route(text)
        if msg_route == TO_CLIENT and chat_id is None:
            transport, chat_id = await self.client_chat(project, prefer=transport)
        async with Session() as s:
            m = Message(
                project_id=project.id, text=text, route=msg_route, kind=kind,
                status="scheduled", transport=transport, chat_id=chat_id,
                # клиенту — с человеческой паузой, своим — сразу
                send_after=utcnow() + (human_delay() if msg_route == TO_CLIENT else dt.timedelta(0)),
            )
            s.add(m)
            await s.commit()
        if msg_route == TO_MANAGER:
            log.info("-> МЕНЕДЖЕРУ (проект %s): %s", project.id, text[:120])
        elif msg_route == TO_OWNER:
            log.info("-> ВЛАДЕЛЬЦУ (проект %s): %s", project.id, text[:120])
        return m

    async def incoming(self, project: Project, text: str,
                       transport: str | None = None, chat_id: str | None = None) -> Message:
        """Сообщение от клиента. Бот не отвечает сам на то, что не его:
        спорное просто пересылается менеджеру, и на этом бот замолкает."""
        msg_route = route(text)
        if msg_route == TO_CLIENT:
            # входящее не может быть «ответом клиенту» — это уже наша реакция
            msg_route = TO_OWNER
        prefix = {TO_MANAGER: "Клиент пишет (это к тебе)", TO_OWNER: "Клиент пишет (техника)"}
        where = f" [{transport}:{chat_id}]" if transport else ""
        return await self.draft(project, f"{prefix[msg_route]}{where}: {text}",
                                kind="forward", force_route=msg_route)

    async def confirm_access_received(self, project: Project, transport=None,
                                      chat_id: str | None = None) -> Message:
        """Клиент прислал доступ. Про то, что мы вырезали из сообщения секрет,
        ему знать незачем — подтверждаем получение и всё."""
        name = getattr(transport, "name", transport)
        return await self.draft(project, "Доступ получен, спасибо. Проверю и напишу.",
                                kind="access_ack", force_route=TO_CLIENT,
                                transport=name, chat_id=chat_id)

    # ---------- отчёт о готовности: один на окно ----------

    async def on_task_done(self, task: Task, project: Project) -> None:
        """Накапливаем в одном неотправленном сообщении вместо письма на задачу."""
        line = f"- {task.title}"
        async with Session() as s:
            pending = (await s.execute(
                select(Message)
                .where(Message.project_id == project.id,
                       Message.kind == "task_done",
                       Message.sent_at.is_(None),
                       Message.status == "scheduled")
                .order_by(Message.id.desc()))).scalars().first()

            if pending is not None:
                pending.text = f"{pending.text}\n{line}"
                pending.route = strictest(pending.route, escalation(task.title))
                await s.commit()
                return

            last_sent = (await s.execute(
                select(Message.sent_at)
                .where(Message.project_id == project.id,
                       Message.kind == "task_done",
                       Message.sent_at.isnot(None))
                .order_by(Message.sent_at.desc()))).scalars().first()

        text = "Готово:\n" + line
        if project.preview_url:
            text += f"\n\nПосмотри: {project.preview_url}"
        m = await self.draft(project, text, kind="task_done")

        # не чаще одного отчёта за окно
        if last_sent is not None:
            floor = last_sent + dt.timedelta(minutes=cfg.aggregate_window_min)
            if m.send_after is None or m.send_after < floor:
                async with Session() as s:
                    stored = await s.get(Message, m.id)
                    stored.send_after = floor
                    await s.commit()
                m.send_after = floor
        return None

    async def on_stage_done(self, project: Project) -> None:
        """Этап закрыт целиком — накопленное уходит не дожидаясь окна."""
        async with Session() as s:
            pending = (await s.execute(
                select(Message)
                .where(Message.project_id == project.id,
                       Message.kind == "task_done",
                       Message.sent_at.is_(None),
                       Message.status == "scheduled")
                .order_by(Message.id.desc()))).scalars().first()
            if pending is None:
                return
            pending.send_after = utcnow()
            await s.commit()

    # ---------- доступы ----------

    async def remind_access(self, project: Project, items: list[AccessItem]) -> Message | None:
        """Одно напоминание на все недостающие пункты, не чаще раза в сутки.

        По пункту на сообщение — это способ, которым бот превращается в спам
        и клиент перестаёт читать вообще всё.
        """
        if not items:
            return None
        async with Session() as s:
            since = utcnow() - dt.timedelta(hours=cfg.access_reminder_h)
            recent = (await s.execute(
                select(Message.id)
                .where(Message.project_id == project.id,
                       Message.kind == "access_reminder",
                       Message.created_at >= since))).first()
        if recent is not None:
            return None

        lines = "\n".join(f"- {i.name} ({i.kind})" for i in items)
        text = ("Чтобы начать, нужен доступ. Не хватает:\n" + lines +
                "\n\nПришли, пожалуйста, одним сообщением.")
        return await self.draft(project, text, kind="access_reminder", force_route=TO_CLIENT)

    async def process(self, task: Task, project: Project) -> None:
        """Слот полосы chat — сюда вешаешь ингест/брифинг/ответы."""
        return None

    # ---------- фоновая отправка ----------

    async def pump(self) -> None:
        while True:
            try:
                await self.pump_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pump failed")
            await asyncio.sleep(15)

    async def pump_once(self) -> int:
        """Отправить всё, чей срок настал. Возвращает число отправленных."""
        now = utcnow()
        quiet = in_quiet_hours()
        sent = 0
        async with Session() as s:
            q = (
                select(Message, Project)
                .join(Project, Project.id == Message.project_id)
                .where(Message.status == "scheduled",
                       Message.send_after.isnot(None),
                       Message.send_after <= now)
            )
            for m, p in (await s.execute(q)).all():
                # тишина касается только клиента: своих будим когда надо
                if quiet and m.route == TO_CLIENT:
                    continue
                try:
                    delivered = await self._deliver(m, p)
                except Exception:
                    log.exception("не смог отправить сообщение %s", m.id)
                    continue      # не помечаем отправленным, попробуем на следующем круге
                if delivered is None:
                    m.status = "cancelled"
                    log.warning("проект %s: маршрут %s без адресата — сообщение %s отменено",
                                p.id, m.route, m.id)
                    continue
                m.status, m.sent_at = "sent", now
                sent += 1
            await s.commit()
        return sent

    async def _deliver(self, m: Message, project: Project) -> str | None:
        """Возвращает id отправленного сообщения либо None, если адресата нет."""
        text = m.text
        if m.route == TO_CLIENT:
            name, chat = m.transport, m.chat_id
            if not chat:
                name, chat = await self.client_chat(project, prefer=m.transport)
            if not chat:
                return None
        else:
            # владелец и менеджер — всегда обычным ботом в Telegram
            name, chat = "telegram", internal_recipient(m.route)

        transport = self.transports.get(name)
        if transport is None:
            await self.send_fn(chat, text)          # заглушка фазы 1
            return chat

        connection_id = None
        if m.route == TO_CLIENT:
            if transport.supports_impersonation():
                connection_id = await self._connection_id(name)
            else:
                # там, где нельзя писать от лица владельца, притворяться запрещено
                text = BOT_SIGNATURE + text
        return await transport.send(chat, text, connection_id=connection_id)
