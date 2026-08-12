from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (JSON, DateTime, ForeignKey, Index, String, TypeDecorator, text,
                        UniqueConstraint, event, select, update)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import cfg

UTC = dt.timezone.utc


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """SQLite не хранит tzinfo: голый DateTime молча теряет offset и отдаёт naive.

    Дальше `utcnow() - row.updated_at` падает с TypeError. Поэтому пишем всегда
    в UTC и naive, а на чтении возвращаем aware. Внутри процесса datetime
    всегда aware-UTC — сравнивать можно что угодно с чем угодно.
    """
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise TypeError(f"ожидался datetime, пришёл {type(value).__name__}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)   # naive трактуем как UTC
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dt.datetime: UtcDateTime}


class Project(Base):
    """Проект = один заказ = одна строка в Google-таблице."""
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    sheet_row: Mapped[int | None] = mapped_column(default=None)

    client: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    # то, что менеджер написал в колонке «Чат клиента»: tg:@user / max:@user / @user
    chat_ref: Mapped[str | None] = mapped_column(default=None)
    # когда клиент последний раз ответил — по этому признаку фаза 4 снимет блокировки
    client_replied_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # бриф собран, уверенности хватает, открытых вопросов нет — третий гейт build
    brief_ready: Mapped[bool] = mapped_column(default=False, server_default=text("0"))

    # new -> briefing -> active -> review -> done | blocked | blocked_access (см. CLAUDE.md)
    status: Mapped[str] = mapped_column(String(20), default="new")
    # галочка менеджера в таблице: пока не True, проект не выходит из briefing
    ready_for_work: Mapped[bool] = mapped_column(default=False)
    priority: Mapped[int] = mapped_column(default=2)          # 1 = высокий, 3 = низкий
    deadline: Mapped[dt.date | None] = mapped_column(default=None)
    price: Mapped[float | None] = mapped_column(default=None)

    workspace: Mapped[str | None] = mapped_column(default=None)
    preview_url: Mapped[str | None] = mapped_column(default=None)
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    cost_usd: Mapped[float] = mapped_column(default=0.0)
    served_units: Mapped[float] = mapped_column(default=0.0)  # счётчик WFQ
    served_at: Mapped[dt.datetime] = mapped_column(default=utcnow)  # к какому моменту он посчитан
    last_action: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    order_idx: Mapped[int] = mapped_column(default=0)

    lane: Mapped[str] = mapped_column(String(10), default="build")
    title: Mapped[str] = mapped_column(String(300), default="")
    prompt: Mapped[str] = mapped_column(default="")
    acceptance: Mapped[list[dict]] = mapped_column(JSON, default=list)

    # pending -> ready -> running -> done | escalated
    status: Mapped[str] = mapped_column(String(15), default="ready")
    attempts: Mapped[int] = mapped_column(default=0)
    cc_session_id: Mapped[str | None] = mapped_column(default=None)
    defects: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_error: Mapped[str] = mapped_column(default="")
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class ProjectChat(Base):
    """Чат проекта в конкретном мессенджере. У проекта их может быть несколько:
    клиент вполне может писать и в Telegram, и в MAX."""
    __tablename__ = "project_chats"
    __table_args__ = (UniqueConstraint("transport", "chat_id", name="uq_project_chat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    transport: Mapped[str] = mapped_column(String(16), default="telegram")
    chat_id: Mapped[str] = mapped_column(String(64))
    handle: Mapped[str | None] = mapped_column(default=None)     # @username или название группы
    is_primary: Mapped[bool] = mapped_column(default=True)
    # в группе бот — отдельный участник и пишет от своего лица, а не от моего.
    # server_default обязателен: миграции вставляют строки сырым SQL, а
    # python-side default в DDL не попадает и ловится как NOT NULL
    is_group: Mapped[bool] = mapped_column(default=False, server_default=text("0"))


class ChatParticipant(Base):
    """Кто сидит в групповом чате. Роль решает, попадут ли слова человека
    в бриф: ТЗ строится по репликам КЛИЕНТА, а не менеджера."""
    __tablename__ = "chat_participants"
    __table_args__ = (UniqueConstraint("transport", "chat_id", "sender_id",
                                       name="uq_chat_participant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transport: Mapped[str] = mapped_column(String(16), default="telegram")
    chat_id: Mapped[str] = mapped_column(String(64))
    sender_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(10), default="client")   # client|manager|owner|bot
    display_name: Mapped[str] = mapped_column(String(200), default="")
    first_seen: Mapped[dt.datetime] = mapped_column(default=utcnow)


class ChatMessage(Base):
    """Переписка с клиентом. Историю не затираем: правки и удаления
    отражаются полями, а не перезаписью."""
    __tablename__ = "chat_messages"
    __table_args__ = (
        # id сообщений уникальны только внутри своего мессенджера
        UniqueConstraint("transport", "chat_id", "tg_message_id", name="uq_chat_message"),
        Index("ix_chat_messages_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transport: Mapped[str] = mapped_column(String(16), default="telegram")
    chat_id: Mapped[str] = mapped_column(String(64))
    tg_message_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), default=None)

    direction: Mapped[str] = mapped_column(String(3), default="in")     # in | out (наследие фазы 2)
    sender_role: Mapped[str] = mapped_column(String(10), default="client",
                                            server_default=text("'client'"))
    sender_id: Mapped[str | None] = mapped_column(default=None)
    text: Mapped[str] = mapped_column(default="")
    has_media: Mapped[bool] = mapped_column(default=False)
    media_kind: Mapped[str | None] = mapped_column(default=None)
    reply_to: Mapped[str | None] = mapped_column(default=None)
    edited_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    deleted: Mapped[bool] = mapped_column(default=False)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class TransportState(Base):
    """Offset поллера. Живёт в БД, поэтому рестарт не теряет и не дублирует."""
    __tablename__ = "transport_state"

    transport: Mapped[str] = mapped_column(String(16), primary_key=True)
    offset: Mapped[str | None] = mapped_column(default=None)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class BusinessConnection(Base):
    """Бизнес-соединение Telegram: от чьего имени бот пишет клиенту."""
    __tablename__ = "business_connections"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    transport: Mapped[str] = mapped_column(String(16), default="telegram")
    user_chat_id: Mapped[str | None] = mapped_column(default=None)
    is_enabled: Mapped[bool] = mapped_column(default=True)
    can_reply: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class AccessItem(Base):
    """Пункт чеклиста доступов. Пока хоть один не verified — проект не строится."""
    __tablename__ = "access_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(200), default="")
    # ftp/ssh/git/hosting_panel/domain/api_key/analytics/design/content/other
    kind: Mapped[str] = mapped_column(String(20), default="other")
    # needed -> requested -> received -> verified | failed
    status: Mapped[str] = mapped_column(String(12), default="needed")
    # ССЫЛКА вида {{SECRET:NAME}}, но НЕ значение — значения живут только в vault
    secret_ref: Mapped[str | None] = mapped_column(default=None)
    requested_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    verified_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    note: Mapped[str] = mapped_column(default="")
    # пункт пропал из брифа: не удаляем, чтобы не потерять уже проверенный доступ
    stale: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    source: Mapped[str] = mapped_column(String(10), default="manual")   # manual|brief


class Message(Base):
    """Исходящее сообщение. Кому именно — решает маршрутизация, см. communicator."""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    text: Mapped[str] = mapped_column(default="")
    # to_client_auto | to_owner | to_manager
    route: Mapped[str] = mapped_column(String(16), default="to_manager")
    # для агрегации и лимитов: task_done | access_reminder | plain | forward
    kind: Mapped[str] = mapped_column(String(20), default="plain")
    # куда отвечать: ответ уходит в тот мессенджер, откуда пришёл вопрос
    transport: Mapped[str | None] = mapped_column(String(16), default=None)
    chat_id: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(12), default="scheduled")  # draft|scheduled|sent|cancelled
    send_after: Mapped[dt.datetime | None] = mapped_column(default=None)
    sent_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(10), default="execute")
    ok: Mapped[bool] = mapped_column(default=False)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    seconds: Mapped[float] = mapped_column(default=0.0)
    log_path: Mapped[str] = mapped_column(default="")
    started_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


engine = create_async_engine(cfg.db_url, echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec) -> None:
    """WAL + busy_timeout: планировщик и синк таблицы пишут в одну базу
    из разных корутин, без этого ловим 'database is locked'."""
    if not cfg.db_url.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()


Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Схему поднимают миграции, а не create_all: с фазы 2 в базе лежит
    живая переписка с клиентами, и «удали БД» перестало быть вариантом."""
    from .migrations import migrate      # поздний импорт: migrations импортирует db
    await migrate()


async def spent_today() -> float:
    """Потрачено за последние 24 часа (скользящее окно, не календарные сутки)."""
    since = utcnow() - dt.timedelta(hours=24)
    async with Session() as s:
        rows = (await s.execute(select(Run.cost_usd).where(Run.started_at >= since))).scalars().all()
    return float(sum(rows))


async def recover_orphan_tasks() -> int:
    """Процесс убили посреди работы — задачи остались в running навсегда.
    На старте возвращаем их в очередь: полос никто не держит, воркеров нет."""
    async with Session() as s:
        res = await s.execute(
            update(Task).where(Task.status == "running").values(status="ready", updated_at=utcnow()))
        await s.commit()
        return res.rowcount


def decayed_units(served: float, served_at: dt.datetime | None, now: dt.datetime | None = None) -> float:
    """Экспоненциальное затухание счётчика WFQ.

    Без него проект, простоявший месяц, возвращается в очередь с огромным
    долгом и вечно проигрывает новичкам. Период полураспада — SERVED_HALFLIFE_H.
    """
    if served <= 0:
        return 0.0
    half = cfg.served_halflife_h
    if half <= 0:
        return served
    now = now or utcnow()
    if served_at is None:
        return served
    hours = (now - served_at).total_seconds() / 3600
    if hours <= 0:
        return served
    return served * (0.5 ** (hours / half))
