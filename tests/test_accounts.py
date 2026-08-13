"""Мультиаккаунтность: два юрлица в одном процессе, ничего не смешивается.

Главный страх тут не технический. Клиент Hustle Design, получивший сообщение
от Qorsa Studio, узнаёт, что подрядчик у него не тот, за кого себя выдавал.
Поэтому изоляция проверяется на всех четырёх стыках: проект, отправка, роли
и таблица.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeCommunicator, make_account, make_project
from sqlalchemy import select, text

from autopilot import accounts as accounts_cfg
from autopilot import roles, sheets
from autopilot.accounts import Account
from autopilot.communicator import Communicator, CrossAccountSend
from autopilot.db import (Account as AccountRow, Base, Message, Project, Session,
                          TransportState, engine, sync_accounts, utcnow)
from autopilot.migrations import migrate
from autopilot.transports.base import load_offset, save_offset


class FakeTransport:
    """Транспорт с компанией. Запоминает, что через него отправляли."""

    def __init__(self, account: str, name: str = "telegram", messages=None,
                 broken: bool = False):
        self.account = account
        self.name = name
        self.sent: list[tuple[str, str]] = []
        self.messages = messages or []
        self.broken = broken
        self.polls = 0

    def supports_impersonation(self) -> bool:
        return False

    async def send(self, chat_ref: str, text: str, connection_id=None) -> str:
        self.sent.append((chat_ref, text))
        return "sent-1"

    async def poll(self):
        self.polls += 1
        if self.broken:
            raise RuntimeError("подключение оборвалось")
        for m in self.messages:
            yield m


# ---------- 1. проект всегда принадлежит компании ----------

async def test_project_belongs_to_account(db):
    """Проект без компании не создаётся — база не даёт."""
    async with Session() as s:
        s.add(Project(client="ничей", title="сирота"))
        with pytest.raises(Exception) as err:
            await s.commit()
    assert "account_id" in str(err.value), f"упало не на компании: {err.value}"

    # а с компанией — заводится
    acc = await make_account("qorsa")
    async with Session() as s:
        s.add(Project(account_id=acc.id, client="Юлия", title="сайт"))
        await s.commit()
        row = (await s.execute(select(Project))).scalars().one()
    assert row.account_id == acc.id


# ---------- 2. ответ уходит только своим ботом ----------

async def test_no_cross_account_send(db, monkeypatch):
    """Сообщение по проекту Hustle не может уйти транспортом Qorsa."""
    monkeypatch.setattr("autopilot.communicator.cfg.quiet_start", 0)
    monkeypatch.setattr("autopilot.communicator.cfg.quiet_end", 0)

    await make_account("qorsa", "Qorsa Studio")
    hustle = await make_account("hustle", "Hustle Design")
    project = await make_project(account="hustle", tg_chat_id="hustle-chat")

    tg_qorsa = FakeTransport("qorsa")
    tg_hustle = FakeTransport("hustle")
    comm = Communicator(transports=[tg_qorsa, tg_hustle])

    m = await comm.draft(project, "выложил превью, посмотри")
    await _due(m.id)
    assert await comm.pump_once() == 1

    assert tg_qorsa.sent == [], "сообщение ушло ботом чужой компании"
    assert len(tg_hustle.sent) == 1
    chat, body = tg_hustle.sent[0]
    assert chat == "hustle-chat"
    # подпись несёт свою компанию и по-прежнему раскрывает, что это бот
    assert "Hustle Design" in body
    assert "Qorsa" not in body
    assert "не человек" in body.lower()
    assert project.account_id == hustle.id


async def test_cross_account_send_raises(db):
    """Прямая попытка отправить чужим транспортом — исключение, а не тихий уход."""
    await make_account("qorsa")
    await make_account("hustle")
    project = await make_project(account="hustle", tg_chat_id="hustle-chat")

    # поднят только бот Qorsa: сообщения по проекту Hustle отправлять нечем
    comm = Communicator(transports=[FakeTransport("qorsa")])
    m = await comm.draft(project, "готово, посмотри")
    await _due(m.id)

    async with Session() as s:
        row = await s.get(Message, m.id)
    with pytest.raises(CrossAccountSend):
        await comm._deliver(row, project)


async def _due(message_id: int) -> None:
    """Сообщение считается созревшим: человеческая задержка в тестах не нужна."""
    async with Session() as s:
        row = await s.get(Message, message_id)
        row.send_after = utcnow()
        await s.commit()


# ---------- 3. роль зависит от компании ----------

def test_role_by_account():
    """Один и тот же id — владелец у себя и посторонний в чужой компании."""
    qorsa = Account(code="qorsa", name="Qorsa Studio", owner_tg_id="7905565788")
    hustle = Account(code="hustle", name="Hustle Design", owner_tg_id="8649452219")

    assert roles.role_of(qorsa, "7905565788") == roles.OWNER
    assert roles.role_of(hustle, "7905565788") == roles.CLIENT, \
        "id Qorsa стал владельцем в чатах Hustle"

    assert roles.role_of(hustle, "8649452219") == roles.OWNER
    assert roles.role_of(qorsa, "8649452219") == roles.CLIENT

    # незнакомый — клиент в обеих, как и раньше
    for acc in (qorsa, hustle):
        assert roles.role_of(acc, "123456") == roles.CLIENT
        assert roles.role_of(acc, None) == roles.CLIENT


async def test_role_by_account_persisted(db):
    """То же самое через remember(): роль пишется относительно компании."""
    qorsa = Account(code="qorsa", name="Qorsa", owner_tg_id="7905565788")
    hustle = Account(code="hustle", name="Hustle", owner_tg_id="8649452219")

    assert await roles.remember(qorsa, "telegram", "chat-q", "7905565788") == roles.OWNER
    assert await roles.remember(hustle, "telegram", "chat-h", "7905565788") == roles.CLIENT


# ---------- 4. два подключения независимы ----------

async def test_two_transports_independent(db, monkeypatch):
    """Offset'ы раздельные, и падение одного бота не останавливает второй."""
    # offset хранится на пару (компания, транспорт)
    await save_offset("telegram", "100", "qorsa")
    await save_offset("telegram", "555", "hustle")

    assert await load_offset("telegram", "qorsa") == "100"
    assert await load_offset("telegram", "hustle") == "555", "offset затёрт чужим ботом"

    await save_offset("telegram", "101", "qorsa")
    assert await load_offset("telegram", "hustle") == "555", \
        "сдвиг одного бота увёл позицию второго"

    async with Session() as s:
        rows = (await s.execute(select(TransportState))).scalars().all()
    assert {(r.account, r.transport) for r in rows} == {
        ("qorsa", "telegram"), ("hustle", "telegram")}

    # падение одного поллера не снимает второй
    from autopilot.ingest import Ingest

    broken = FakeTransport("qorsa", broken=True)
    alive = FakeTransport("hustle")
    accounts = [Account(code="qorsa", name="Q", owner_tg_id="1"),
                Account(code="hustle", name="H", owner_tg_id="2")]
    ing = Ingest([broken, alive], FakeCommunicator(), accounts=accounts)

    # backoff по умолчанию стартует с секунды — для теста это вечность
    monkeypatch.setattr("autopilot.ingest.Backoff",
                        lambda *a, **k: _FastBackoff())

    task = asyncio.create_task(ing.run())
    await asyncio.sleep(0.35)          # дать циклам покрутиться
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert broken.polls >= 1, "упавший поллер даже не стартовал"
    assert alive.polls >= 1, "живой поллер не запустился"
    # именно это и требовалось: сломанный перезапускается, а не роняет процесс
    assert broken.polls >= 2, "упавший поллер не переподключался"


# ---------- 5. таблицы не смешиваются ----------

class _FastBackoff:
    """Backoff без ожидания: проверяем сам факт переподключения, а не паузы."""

    def reset(self):
        pass

    async def sleep(self, forced=None):
        await asyncio.sleep(0.01)


class FakeWorksheet:
    def __init__(self, header, records):
        self._header = header
        self._records = records
        self.updates: list[dict] = []

    def get_all_records(self):
        return self._records

    def row_values(self, n):
        return self._header

    def batch_update(self, data):
        self.updates.extend(data)


async def test_sheet_isolation(db, monkeypatch):
    """Синк одной компании не переносит к себе проекты другой."""
    qorsa = await make_account("qorsa", "Qorsa Studio")
    hustle = await make_account("hustle", "Hustle Design")

    # проект Hustle уже в базе, у него ID 1 в таблице Hustle
    project = await make_project(account="hustle", title="сайт Hustle")

    header = ["ID", "Клиент", "Проект", "Чат клиента", "Цена", "Дедлайн",
              "Приоритет", "Папка", "Готов к работе", "Статус", "Прогресс",
              "Ждём от клиента", "Превью", "Последнее действие", "Стоимость $",
              "Обновлено"]
    # в ТАБЛИЦЕ QORSA кто-то по ошибке вписал ID чужого проекта
    ws = FakeWorksheet(header, [{**{c: "" for c in header},
                                 "ID": str(project.id), "Клиент": "Чужой",
                                 "Проект": "перехват"}])
    monkeypatch.setattr(sheets, "_client", lambda *a, **k: ws)

    sync = sheets.SheetSync(account=qorsa, account_id=qorsa.id)
    await sync._pull()

    async with Session() as s:
        row = await s.get(Project, project.id)
    assert row.account_id == hustle.id, "проект увели в чужую компанию"
    assert row.title == "сайт Hustle", "чужой синк переписал поля проекта"

    # и push тоже видит только свои строки
    await sync._push()
    assert ws.updates == [], "push разложил чужой проект по своей таблице"


async def test_sheet_shared_table_splits_by_column(db, monkeypatch):
    """Одна таблица на две компании: строки различаются колонкой «Компания»."""
    qorsa = await make_account("qorsa", "Qorsa Studio")
    await make_account("hustle", "Hustle Design")

    header = ["ID", "Компания", "Клиент", "Проект", "Чат клиента", "Цена",
              "Дедлайн", "Приоритет", "Папка", "Готов к работе", "Статус",
              "Прогресс", "Ждём от клиента", "Превью", "Последнее действие",
              "Стоимость $", "Обновлено"]
    base = {c: "" for c in header}
    ws = FakeWorksheet(header, [
        {**base, "Компания": "qorsa", "Клиент": "Юлия", "Проект": "лендинг"},
        {**base, "Компания": "hustle", "Клиент": "Другой", "Проект": "не моё"},
    ])
    monkeypatch.setattr(sheets, "_client", lambda *a, **k: ws)

    await sheets.SheetSync(account=qorsa, account_id=qorsa.id, shared=True)._pull()

    async with Session() as s:
        rows = (await s.execute(select(Project))).scalars().all()
    assert len(rows) == 1, "синк Qorsa подобрал строку Hustle"
    assert rows[0].client == "Юлия"
    assert rows[0].account_id == qorsa.id


# ---------- 6. миграция ----------

async def test_migration_assigns_qorsa(db):
    """Проекты, заведённые до мультиаккаунтности, уезжают в qorsa."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS schema_version"))
        await conn.run_sync(Base.metadata.create_all)
        # база «до фазы 6»: компаний нет, у проектов нет привязки
        await conn.execute(text("DROP TABLE IF EXISTS accounts"))
        info = (await conn.execute(text("PRAGMA table_info(projects)"))).all()
        defs, names = [], []
        for _, cname, ctype, notnull, dflt, pk in info:
            if cname == "account_id":
                continue
            names.append(cname)
            piece = f'"{cname}" {ctype}'
            if pk:
                piece += " PRIMARY KEY"
            elif notnull:
                piece += " NOT NULL"
            if dflt is not None:
                piece += f" DEFAULT {dflt}"
            defs.append(piece)
        cols = ", ".join(f'"{n}"' for n in names)
        await conn.execute(text("ALTER TABLE projects RENAME TO projects_old"))
        await conn.execute(text(f"CREATE TABLE projects ({', '.join(defs)})"))
        await conn.execute(text(
            "INSERT INTO projects (id, client, title, status, priority, "
            "ready_for_work, brief_ready, cost_usd, served_units, served_at, "
            "brief, last_action, updated_at) "
            "VALUES (1, 'Юлия', 'сайт', 'active', 2, 1, 0, 0, 0, "
            "'2026-01-01 00:00:00', '{}', '', '2026-01-01 00:00:00')"))
        await conn.execute(text("DROP TABLE projects_old"))
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, name TEXT DEFAULT '', "
            "applied_at TEXT DEFAULT (datetime('now')))"))
        await conn.execute(text(
            "INSERT INTO schema_version (version, name) VALUES (5, 'до компаний')"))

    await migrate()

    async with Session() as s:
        proj = await s.get(Project, 1)
        acc = await s.get(AccountRow, proj.account_id)
    assert acc is not None and acc.code == "qorsa"
    assert proj.title == "сайт", "миграция потеряла данные проекта"


# ---------- конфиг ----------

def test_third_company_needs_no_code(tmp_path):
    """Третья компания добавляется блоком в TOML, без правок кода."""
    path = tmp_path / "accounts.toml"
    path.write_text(
        '[[account]]\ncode = "qorsa"\nname = "Qorsa Studio"\nowner_tg_id = "1"\n\n'
        '[[account]]\ncode = "hustle"\nname = "Hustle Design"\nowner_tg_id = "2"\n\n'
        '[[account]]\ncode = "third"\nname = "Третья"\nowner_tg_id = "3"\n'
        'active = false\n',
        encoding="utf-8")

    loaded = accounts_cfg.load(path)
    assert [a.code for a in loaded] == ["qorsa", "hustle", "third"]
    assert [a.code for a in accounts_cfg.active(path)] == ["qorsa", "hustle"]
    assert accounts_cfg.by_code("hustle", path).owner_tg_id == "2"
    # подпись по умолчанию — имя компании, а не пустая строка
    assert accounts_cfg.by_code("qorsa", path).signature == "Qorsa Studio"


def test_duplicate_code_is_error(tmp_path):
    """Два блока с одним кодом — молча потерянная компания. Падаем."""
    path = tmp_path / "accounts.toml"
    path.write_text('[[account]]\ncode = "qorsa"\nname = "A"\n\n'
                    '[[account]]\ncode = "qorsa"\nname = "Б"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="повторяется"):
        accounts_cfg.load(path)


def test_typo_in_field_is_error(tmp_path):
    """Опечатка в имени поля не должна проходить молча: sheet_di вместо sheet_id
    увёл бы синк не в ту таблицу."""
    path = tmp_path / "accounts.toml"
    path.write_text('[[account]]\ncode = "q"\nname = "Q"\nsheet_di = "xxx"\n',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="непонятные поля"):
        accounts_cfg.load(path)


def test_no_token_in_config(tmp_path):
    """В конфиге лежит ССЫЛКА на секрет, а не сам токен."""
    path = tmp_path / "accounts.toml"
    path.write_text('[[account]]\ncode = "q"\nname = "Q"\n'
                    'bot_token_ref = "QORSA_BOT_TOKEN"\n', encoding="utf-8")
    acc = accounts_cfg.load(path)[0]
    assert acc.bot_token_ref == "QORSA_BOT_TOKEN"
    assert not hasattr(acc, "bot_token"), "у компании не должно быть поля со значением токена"


async def test_sync_accounts_keeps_orphans(db):
    """Компания, пропавшая из конфига, не удаляется: на неё ссылаются проекты."""
    ids = await sync_accounts([Account(code="qorsa", name="Q", owner_tg_id="1"),
                               Account(code="hustle", name="H", owner_tg_id="2")])
    assert set(ids) == {"qorsa", "hustle"}

    ids2 = await sync_accounts([Account(code="qorsa", name="Q", owner_tg_id="1")])
    assert "hustle" in ids2, "компания исчезла из базы вместе со своими проектами"
    async with Session() as s:
        row = (await s.execute(
            select(AccountRow).where(AccountRow.code == "hustle"))).scalars().one()
    assert row.active is False, "пропавшая компания осталась активной"


# ---------- планировщик: не трогали, но проверяем, что не смешивает ----------

async def test_scheduler_serves_both_companies(db):
    """WFQ раздаёт слоты по проектам независимо от компании — и обе получают.

    Планировщик компанию не знает и знать не должен: очередь общая, потому
    что процесс и машина общие. Проверяем, что при этом ни одна компания
    не оказывается заперта проектом другой.
    """
    from autopilot.fakes import FakeExecutor, FakeVerifier
    from autopilot.scheduler import Scheduler
    from autopilot.db import Task

    await make_account("qorsa")
    await make_account("hustle")
    q = await make_project(account="qorsa", title="проект Qorsa")
    h = await make_project(account="hustle", title="проект Hustle")

    async with Session() as s:
        for project in (q, h):
            for i in range(3):
                s.add(Task(project_id=project.id, lane="build", status="ready",
                           title=f"{project.title} шаг {i}", order_idx=i,
                           verify_class="auto", executor="claude_code",
                           acceptance=[], depends_on=[]))
        await s.commit()

    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    for _ in range(4):
        await sched.tick()
        await sched.drain()

    served = set(ex.served)
    assert q.id in served and h.id in served, \
        f"одна из компаний не получила ни одного слота: {ex.served}"


async def test_budget_is_shared_between_companies(db, monkeypatch):
    """Суточный бюджет ОБЩИЙ — кошелёк у компаний один.

    Фиксируем это тестом не потому, что так лучше, а потому, что это
    неочевидно: перерасход на проектах Hustle останавливает build и у Qorsa.
    Если однажды понадобится бюджет на компанию, тест покажет, что менять.
    """
    from autopilot import scheduler as sched_mod
    from autopilot.fakes import FakeExecutor, FakeVerifier
    from autopilot.scheduler import Scheduler
    from autopilot.db import Task

    await make_account("qorsa")
    await make_account("hustle")
    q = await make_project(account="qorsa")
    h = await make_project(account="hustle")
    async with Session() as s:
        for project in (q, h):
            s.add(Task(project_id=project.id, lane="build", status="ready",
                       title=f"задача {project.id}", verify_class="auto",
                       executor="claude_code", acceptance=[], depends_on=[]))
        await s.commit()

    monkeypatch.setattr(sched_mod, "spent_today", lambda: _over_budget())
    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()

    assert ex.served == [], "перерасход не остановил build ни у одной компании"
    assert sched.budget_paused is True


async def _over_budget() -> float:
    from autopilot.config import cfg
    return cfg.daily_budget_usd + 1
