from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import (JSON, DateTime, ForeignKey, Index, String, TypeDecorator, text,
                        UniqueConstraint, event, select, update)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import cfg

log_db = logging.getLogger("db")

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


class Account(Base):
    """Компания владельца: Qorsa Studio, Hustle Design, дальше по списку.

    Строка тут — зеркало блока из `accounts.toml`, а не источник правды:
    конфиг правится руками, а таблица нужна затем, чтобы у проекта был
    честный внешний ключ, а у отчётов — join без чтения файла.
    Синхронизируется на старте по `code`.

    Токен бота хранится НЕ здесь: в `bot_token_ref` лежит имя секрета
    в vault (см. accounts.py).
    """
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    # server_default у всех строковых: миграция вставляет компанию по умолчанию
    # сырым SQL, где питоновские default= не работают
    name: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    # как компанию зовут люди — в таблице и в общении с клиентом
    display_name: Mapped[str] = mapped_column(String(200), default="",
                                              server_default=text("''"))
    # что может стоять в колонке «Компания» и означать эту компанию
    sheet_alias: Mapped[list] = mapped_column(JSON, default=list,
                                              server_default=text("'[]'"))
    # основной мессенджер: новый проект получает канал без догадок
    transport: Mapped[str] = mapped_column(String(16), default="telegram",
                                           server_default=text("'telegram'"))
    handle: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    owner_tg_id: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    owner_max_id: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    bot_token_ref: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    sheet_id: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    sheet_tab: Mapped[str] = mapped_column(String(64), default="Заказы",
                                           server_default=text("'Заказы'"))
    signature: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    active: Mapped[bool] = mapped_column(default=True, server_default=text("1"))
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, server_default=text("CURRENT_TIMESTAMP"))

    # Строка БД и объект из accounts.toml должны быть взаимозаменяемы: код
    # выше по стеку берёт компанию то из конфига, то из базы и не обязан
    # знать, что именно ему досталось. Разъедутся — получим AttributeError
    # в проде, а не в тесте
    @property
    def title(self) -> str:
        return str(self.display_name or self.name)

    @property
    def aliases(self) -> tuple[str, ...]:
        out = [self.code, self.name, self.display_name, *(self.sheet_alias or [])]
        return tuple(str(x) for x in out if str(x or "").strip())


class Project(Base):
    """Проект = один заказ = одна строка в Google-таблице."""
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Проект ВСЕГДА принадлежит ровно одной компании. Не nullable намеренно:
    # проект без компании невозможно ни синкать в таблицу, ни ответить по нему
    # клиенту — непонятно, с какого бота. Лучше отказ на вставке, чем сирота
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    sheet_row: Mapped[int | None] = mapped_column(default=None)

    client: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    # то, что менеджер написал в колонке «Чат клиента»: tg:@user / max:@user / @user
    chat_ref: Mapped[str | None] = mapped_column(default=None)
    # когда клиент последний раз ответил — по этому признаку фаза 4 снимет блокировки
    client_replied_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    # бриф собран, уверенности хватает, открытых вопросов нет — третий гейт build
    brief_ready: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    # доля задач, которые система может закрыть без человека. Считается
    # планировщиком и нужна ДО начала работы, а не после
    autonomy_ratio: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    # та же доля, но по времени: мелких ручных задач может быть много,
    # а времени они занимать мало — и наоборот
    autonomy_ratio_time: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    planned_at: Mapped[dt.datetime | None] = mapped_column(default=None)

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

    # pending -> ready -> running -> done | escalated | needs_human
    status: Mapped[str] = mapped_column(String(15), default="ready")

    # --- планирование (фаза 4) ---
    # откуда взялась задача: текст пункта брифа. Без него задача — выдумка
    deliverable_ref: Mapped[str | None] = mapped_column(default=None)
    # чем проверяем результат: auto | assisted | human
    verify_class: Mapped[str] = mapped_column(String(10), default="human",
                                              server_default=text("'human'"))
    # кто делает: claude_code | manual | external
    executor: Mapped[str] = mapped_column(String(12), default="manual",
                                          server_default=text("'manual'"))
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    estimate_min: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    risk: Mapped[str] = mapped_column(default="")
    # требование исчезло из брифа — задачу не удаляем, а показываем человеку
    orphaned: Mapped[bool] = mapped_column(default=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(default=0)
    cc_session_id: Mapped[str | None] = mapped_column(default=None)
    defects: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Замечания судьи ВНЕ заявленного критерия. В defects им не место:
    # оттуда задача уходит на повтор и в эскалацию, а «нет тестов» при
    # выполненном критерии — повод завести новую задачу, а не завалить эту
    observations: Mapped[list[str]] = mapped_column(JSON, default=list,
                                                    server_default=text("'[]'"))
    last_error: Mapped[str] = mapped_column(default="")
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


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
    """Offset поллера. Живёт в БД, поэтому рестарт не теряет и не дублирует.

    Ключ составной: у каждой компании свой бот, свой поток апдейтов и свой
    offset. С общим ключом по имени транспорта два бота затирали бы позицию
    друг друга — и половина сообщений уезжала бы в никуда при каждом рестарте.
    """
    __tablename__ = "transport_state"

    account: Mapped[str] = mapped_column(String(32), primary_key=True,
                                         server_default=text("'qorsa'"))
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
    # Стоимость. У CLI это ОЦЕНКА, а не списание: подписка деньгами
    # не считается, и складывать её с расходом API — значит соврать
    # в обе стороны сразу.
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    # чем платили: api (реальные деньги) | cli (квота подписки)
    backend: Mapped[str] = mapped_column(String(8), default="api",
                                         server_default=text("'api'"))
    seconds: Mapped[float] = mapped_column(default=0.0)
    log_path: Mapped[str] = mapped_column(default="")
    # ВРЕМЯ НАЧАЛА, и теперь по-настоящему: строка заводится ДО вызова модели.
    # Раньше она создавалась после, и started_at был временем окончания —
    # мелочь, которая путала разбор длительностей и прятала оборванные
    # прогоны: у брошенного вызова строки не появлялось вовсе
    started_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    # Пусто — значит вызов не закончился: либо идёт прямо сейчас, либо
    # процесс убили на середине. Отличать эти два состояния от нормального
    # прогона нужно глазами, поэтому колонка отдельная
    finished_at: Mapped[dt.datetime | None] = mapped_column(default=None)


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


async def sync_accounts(accounts) -> dict[str, int]:
    """Переносит компании из accounts.toml в таблицу. Возвращает {code: id}.

    Конфиг — источник правды, таблица — его зеркало ради внешнего ключа
    у проекта. Компанию, пропавшую из конфига, НЕ удаляем: на неё ссылаются
    живые проекты, и удаление оборвало бы связь. Помечаем неактивной.
    """
    codes = {}
    async with Session() as s:
        existing = {a.code: a for a in
                    (await s.execute(select(Account))).scalars().all()}
        seen = set()
        for acc in accounts:
            seen.add(acc.code)
            row = existing.get(acc.code)
            if row is None:
                row = Account(code=acc.code)
                s.add(row)
            row.name = acc.name
            row.display_name = getattr(acc, "display_name", "") or acc.name
            row.sheet_alias = list(getattr(acc, "sheet_alias", ()) or ())
            row.transport = getattr(acc, "transport", "telegram")
            row.handle = acc.handle
            row.owner_tg_id = str(acc.owner_tg_id or "")
            row.owner_max_id = str(acc.owner_max_id or "")
            row.bot_token_ref = acc.bot_token_ref
            row.sheet_id = str(acc.sheet_id or "")
            row.sheet_tab = acc.sheet_tab
            row.signature = acc.signature
            row.active = bool(acc.active)
            row.updated_at = utcnow()
        for code, row in existing.items():
            if code not in seen and row.active:
                row.active = False
                log_db.warning("компания %s пропала из конфига — помечена неактивной "
                               "(проекты на ней остаются)", code)
        await s.commit()
        for a in (await s.execute(select(Account))).scalars().all():
            codes[a.code] = a.id
    return codes


async def account_code(account_id: int | None) -> str:
    """id компании -> её код. Пустая строка, если компании нет."""
    if not account_id:
        return ""
    async with Session() as s:
        row = await s.get(Account, int(account_id))
    return row.code if row else ""


async def account_signature(account_id: int | None) -> str:
    """Как бот подписывается клиенту от лица этой компании.

    Компания добавляется К раскрытию «я бот», а не ВМЕСТО него. Клиент должен
    понимать две вещи: с какой из компаний он говорит и что отвечает не человек.
    Подставить сюда одно только «Qorsa Studio» значит убрать второе — а это
    ровно то, ради чего подпись заводилась.
    """
    if not account_id:
        return BOT_SIGNATURE_FALLBACK
    async with Session() as s:
        row = await s.get(Account, int(account_id))
    name = (row.signature or row.name) if row else ""
    if not name:
        return BOT_SIGNATURE_FALLBACK
    return f"🤖 {name}: бот по техническим вопросам (не человек).\n\n"


BOT_SIGNATURE_FALLBACK = "🤖 Бот по техническим вопросам (не человек).\n\n"


async def open_run(task_id: int, kind: str) -> int:
    """Завести строку расхода ДО вызова модели и вернуть её id.

    Так `started_at` означает то, что написано, а вызов, оборванный на
    середине, оставляет след: строка есть, `finished_at` пуст. Раньше строка
    появлялась после ответа, и прерванный прогон выглядел так, будто его
    не было — при том, что квоту он сжёг.
    """
    async with Session() as s:
        row = Run(task_id=task_id, kind=kind, ok=False, started_at=utcnow())
        s.add(row)
        await s.commit()
        return row.id


async def close_run(run_id: int, *, ok: bool, backend: str, cost_usd: float,
                    seconds: float, log_path: str = "") -> None:
    """Дописать в строку то, что стало известно после вызова."""
    async with Session() as s:
        row = await s.get(Run, run_id)
        if row is None:
            return
        row.ok = ok
        row.backend = backend
        row.cost_usd = cost_usd
        row.seconds = seconds
        if log_path:
            row.log_path = log_path
        row.finished_at = utcnow()
        await s.commit()


async def spent_today() -> float:
    """РЕАЛЬНЫЕ деньги за последние 24 часа (скользящее окно).

    Только `backend="api"`. Расход подписки сюда не входит намеренно:
    иначе суточный бюджет вставал бы на пустом месте — всё идёт через CLI,
    денег не тратится ни рубля, а работа остановлена.
    """
    since = utcnow() - dt.timedelta(hours=24)
    async with Session() as s:
        rows = (await s.execute(
            select(Run.cost_usd).where(Run.started_at >= since,
                                       Run.backend == "api"))).scalars().all()
    return float(sum(rows))


async def consumed_today() -> dict[str, float]:
    """Обе цифры раздельно: деньги и подписка.

    Показывать их одной суммой нельзя — это разные ресурсы с разными
    потолками. Кошелёк меряется в долларах, окно подписки — в вызовах
    и времени до сброса.
    """
    since = utcnow() - dt.timedelta(hours=24)
    async with Session() as s:
        rows = (await s.execute(
            select(Run.backend, Run.cost_usd, Run.seconds)
            .where(Run.started_at >= since))).all()
    out = {"api_usd": 0.0, "cli_usd_est": 0.0, "api_calls": 0.0,
           "cli_calls": 0.0, "cli_seconds": 0.0}
    for backend, cost, seconds in rows:
        if str(backend) == "cli":
            out["cli_usd_est"] += float(cost or 0)
            out["cli_calls"] += 1
            out["cli_seconds"] += float(seconds or 0)
        else:
            out["api_usd"] += float(cost or 0)
            out["api_calls"] += 1
    return out


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
