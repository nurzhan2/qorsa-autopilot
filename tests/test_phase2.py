"""Миграции, импорт истории и исходящая маршрутизация по транспортам."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from conftest import add_chat, make_project
from sqlalchemy import func, select, text

from autopilot.communicator import BOT_SIGNATURE, TO_CLIENT, Communicator
from autopilot.config import cfg
from autopilot.db import (Account, Base, BusinessConnection, ChatMessage, Message, Project,
                          ProjectChat, Session, engine, utcnow)
from autopilot.ingest import Ingest
from autopilot.migrations import MIGRATIONS, current_version, migrate
from autopilot.transports.base import InboundMessage

ROOT = Path(__file__).resolve().parent.parent
LATEST = MIGRATIONS[-1][0]


# ---------- миграции ----------

async def test_migrations_idempotent(db):
    """Повторный старт не ломает схему: с пустой БД и с БД предыдущей версии."""
    # с пустой базы
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS schema_version"))

    assert await migrate() == LATEST
    assert await migrate() == LATEST, "повторный прогон сдвинул версию"
    assert await migrate() == LATEST

    async with engine.begin() as conn:
        assert await current_version(conn) == LATEST
        # схема на месте и рабочая
        await conn.execute(text("SELECT id, chat_ref, client_replied_at FROM projects"))
        await conn.execute(text("SELECT transport, chat_id, tg_message_id FROM chat_messages"))
        await conn.execute(text("SELECT transport, chat_id FROM messages"))


async def test_migration_from_previous_version(db):
    """База фазы 1 доезжает до актуальной: колонки добавляются, tg_chat_id переезжает."""
    # Воспроизводим базу фазы 1 не переписывая её схему руками: берём актуальную
    # и откатываем ровно то, что появилось в фазе 2. Так фикстура не разъедется
    # с настоящей схемой при следующей правке моделей.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS schema_version"))
        await conn.run_sync(Base.metadata.create_all)
        for table in ("chat_messages", "project_chats", "transport_state",
                      "business_connections", "chat_participants", "accounts"):
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        # Фазы 6 тогда тоже не было. DROP COLUMN тут не работает: SQLite
        # отказывается удалять колонку, на которой висит внешний ключ.
        # Поэтому пересобираем таблицу из её же PRAGMA, выкинув account_id —
        # так фикстура по-прежнему не разъезжается с настоящей схемой
        info = (await conn.execute(text("PRAGMA table_info(projects)"))).all()
        defs, names = [], []
        for _, cname, ctype, notnull, dflt, pk in info:
            if cname == "account_id":
                continue
            names.append(cname)
            piece = f'"{cname}" {ctype}'
            if pk:
                piece += " PRIMARY KEY"
            if notnull and not pk:
                piece += " NOT NULL"
            if dflt is not None:
                piece += f" DEFAULT {dflt}"
            defs.append(piece)
        await conn.execute(text("ALTER TABLE projects RENAME TO projects_v6"))
        await conn.execute(text(f"CREATE TABLE projects ({', '.join(defs)})"))
        cols = ", ".join(f'"{n}"' for n in names)
        await conn.execute(text(f"INSERT INTO projects ({cols}) SELECT {cols} FROM projects_v6"))
        await conn.execute(text("DROP TABLE projects_v6"))
        for column in ("chat_ref", "client_replied_at", "brief_ready"):
            await conn.execute(text(f"ALTER TABLE projects DROP COLUMN {column}"))
        await conn.execute(text("ALTER TABLE projects ADD COLUMN tg_chat_id VARCHAR"))
        for column in ("transport", "chat_id"):
            await conn.execute(text(f"ALTER TABLE messages DROP COLUMN {column}"))
        for column in ("stale", "source"):
            await conn.execute(text(f"ALTER TABLE access_items DROP COLUMN {column}"))
        await conn.execute(text(
            "INSERT INTO projects (id, client, title, tg_chat_id, status, priority, "
            "ready_for_work, cost_usd, served_units, served_at, brief, last_action, updated_at) "
            "VALUES (1, 'Иван', 'сайт', '555000', 'active', 2, 1, 0, 0, "
            "'2026-01-01 00:00:00', '{}', '', '2026-01-01 00:00:00')"))
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, name TEXT DEFAULT '', "
            "applied_at TEXT DEFAULT (datetime('now')))"))
        await conn.execute(text(
            "INSERT INTO schema_version (version, name) VALUES (1, 'baseline')"))

    assert await migrate() == LATEST

    async with Session() as s:
        chat = (await s.execute(select(ProjectChat))).scalars().one()
        proj = await s.get(Project, 1)
    assert chat.project_id == 1
    assert chat.chat_id == "555000"
    assert chat.transport == "telegram"
    assert proj.chat_ref == "tg:555000", "старое значение не перенеслось в chat_ref"

    # фаза 6: проект, заведённый до мультиаккаунтности, уезжает в qorsa
    async with Session() as s:
        acc = await s.get(Account, proj.account_id)
    assert acc is not None and acc.code == "qorsa"

    # и повторный прогон ничего не сломает и не задвоит
    assert await migrate() == LATEST
    async with Session() as s:
        n = (await s.execute(select(func.count()).select_from(ProjectChat))).scalar_one()
    assert n == 1


# ---------- импорт истории ----------

def load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_tg_export", ROOT / "scripts" / "import_tg_export.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def export_fixture(path: Path, chat_id: int, messages: list[dict]) -> Path:
    payload = {"name": "Иван", "type": "personal_chat", "id": chat_id, "messages": messages}
    f = path / "result.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return f


async def test_import_no_duplicates(db, tmp_path):
    """Импорт экспорта поверх live-сообщений не создаёт дублей."""
    p = await make_project(tg_chat_id="777")
    importer = load_importer()

    # сначала сообщение прилетело живым потоком
    await Ingest([], _Silent()).handle(
        _FakeTr(), InboundMessage(transport="telegram", chat_id="777", message_id="2",
                                  text="это уже есть", cursor="1"))

    f = export_fixture(tmp_path, 777, [
        {"id": 1, "type": "message", "date_unixtime": "1770000000",
         "from_id": "user777", "text": "первое, старое"},
        {"id": 2, "type": "message", "date_unixtime": "1770000100",
         "from_id": "user777", "text": "это уже есть"},
        {"id": 3, "type": "message", "date_unixtime": "1770000200",
         "from_id": "user777", "text": [{"type": "link", "text": "http://x.kz"}, " смотри"]},
        {"id": 4, "type": "service", "action": "phone_call"},
    ])

    assert await importer.run(f, None, None, False) == 0
    # повторный импорт того же файла тоже не должен задвоить
    assert await importer.run(f, None, None, False) == 0

    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage).order_by(ChatMessage.tg_message_id))).scalars().all()

    assert len(rows) == 3, f"ожидалось 3 сообщения, получено {len(rows)}"
    assert [r.tg_message_id for r in rows] == ["1", "2", "3"]
    assert all(r.project_id == p.id for r in rows)
    assert rows[2].text == "http://x.kz смотри", "разметка текста не склеилась"


async def test_import_intercepts_secrets(db, tmp_path, monkeypatch):
    """Пароль из старой переписки в базу открытым не попадает."""
    from cryptography.fernet import Fernet

    from autopilot.vault import Vault
    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    monkeypatch.setattr("autopilot.secrets_scan.default_vault", v)

    await make_project(tg_chat_id="777")
    importer = load_importer()
    f = export_fixture(tmp_path, 777, [
        {"id": 1, "type": "message", "date_unixtime": "1770000000", "from_id": "user777",
         "text": "ftp://ivan:H1stor1cP@ss@old.example.kz"},
    ])
    await importer.run(f, None, None, False)

    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()
    assert "H1stor1cP@ss" not in row.text
    assert "{{SECRET:" in row.text


# ---------- исходящая маршрутизация ----------

class RecordingTransport:
    def __init__(self, name: str, impersonation: bool):
        self.name = name
        self._imp = impersonation
        self.sent: list[tuple[str, str, str | None]] = []

    def supports_impersonation(self) -> bool:
        return self._imp

    async def send(self, chat_ref, text, connection_id=None) -> str:
        self.sent.append((chat_ref, text, connection_id))
        return "1"

    async def resolve_chat(self, handle):
        return None

    async def poll(self):                       # pragma: no cover
        return
        yield


@pytest.fixture
def tg():
    return RecordingTransport("telegram", impersonation=True)


@pytest.fixture
def mx():
    return RecordingTransport("max", impersonation=False)


async def due(message_id: int) -> None:
    async with Session() as s:
        m = await s.get(Message, message_id)
        m.send_after = utcnow()
        await s.commit()


async def test_outbound_routing(db, tg, monkeypatch):
    """Клиенту — через business_connection_id, себе — обычным ботом."""
    monkeypatch.setattr(cfg, "tg_owner", "owner-chat")
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    p = await make_project(tg_chat_id="client-chat")
    async with Session() as s:
        s.add(BusinessConnection(id="bizconn-1", transport="telegram",
                                 is_enabled=True, can_reply=True))
        await s.commit()

    comm = Communicator(transports=[tg])
    to_client = await comm.draft(p, "выложил превью, посмотри")
    to_owner = await comm.draft(p, "клиент спрашивает про скидку")
    assert to_client.route == TO_CLIENT
    await due(to_client.id)
    await due(to_owner.id)

    assert await comm.pump_once() == 2
    by_chat = {chat: (text, conn) for chat, text, conn in tg.sent}

    assert "client-chat" in by_chat
    assert by_chat["client-chat"][1] == "bizconn-1", "клиенту ушло не от лица владельца"
    assert BOT_SIGNATURE not in by_chat["client-chat"][0], "в Telegram подпись бота не нужна"

    # коммерция уходит владельцу обычным ботом: бизнес-соединение — только клиенту
    assert "owner-chat" in by_chat
    assert by_chat["owner-chat"][1] is None, "своим ушло через бизнес-соединение"


async def test_no_impersonation_in_max(db, mx, monkeypatch):
    """В MAX бизнес-режима нет — сообщение клиенту помечено как ботовское."""
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    p = await make_project(tg_chat_id="max-chat", transport="max")

    comm = Communicator(transports=[mx])
    m = await comm.draft(p, "выложил превью, посмотри")
    await due(m.id)
    assert await comm.pump_once() == 1

    chat, sent_text, conn = mx.sent[0]
    assert chat == "max-chat"
    assert conn is None
    # подпись теперь несёт и компанию, и раскрытие «не человек» — второе
    # обязательно: компания без него превратила бы подпись в обычный бренд
    assert sent_text.startswith("🤖"), "бот выдаёт себя за человека"
    assert "не человек" in sent_text.lower()
    assert "qorsa" in sent_text.lower(), "клиент не видит, с какой компанией говорит"


async def test_project_two_chats(db, tg, mx, monkeypatch):
    """Ответ уходит в тот мессенджер, откуда пришёл вопрос."""
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    p = await make_project(tg_chat_id="tg-chat")            # основной — telegram
    await add_chat(p.id, "max", "max-chat")

    comm = Communicator(transports=[tg, mx])

    # вопрос пришёл из MAX — подтверждение получения уходит туда же
    m_max = await comm.confirm_access_received(p, mx, "max-chat")
    await due(m_max.id)
    assert await comm.pump_once() == 1
    assert [c for c, _, _ in mx.sent] == ["max-chat"]
    assert tg.sent == []

    # вопрос из Telegram — ответ в Telegram
    m_tg = await comm.confirm_access_received(p, tg, "tg-chat")
    await due(m_tg.id)
    assert await comm.pump_once() == 1
    assert [c for c, _, _ in tg.sent] == ["tg-chat"]
    assert len(mx.sent) == 1, "ответ ушёл не в тот мессенджер"

    # без указания источника — в основной чат проекта
    m_default = await comm.draft(p, "выложил превью")
    await due(m_default.id)
    assert await comm.pump_once() == 1
    assert [c for c, _, _ in tg.sent] == ["tg-chat", "tg-chat"]


class _Silent:
    async def notify_owner(self, text): ...
    async def confirm_access_received(self, project, transport=None, chat_id=None): ...
    async def incoming(self, project, text, transport=None, chat_id=None,
                       in_group=False): ...


class _FakeTr:
    name = "telegram"

    def supports_impersonation(self): return True
    async def send(self, chat_ref, text, connection_id=None): return "1"
    async def resolve_chat(self, handle): return None
