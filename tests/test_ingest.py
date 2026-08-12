"""Приём сообщений: offset, дедуп, правки, привязка, перехват секретов.

Сети здесь нет ни в одном тесте — транспорт подменён фикстурами апдейтов.
"""
from __future__ import annotations

import datetime as dt

import pytest
from conftest import add_chat, make_project
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from autopilot.config import cfg
from autopilot.db import ChatMessage, Project, ProjectChat, Session, Task, TransportState
from autopilot.ingest import Ingest, parse_chat_ref
from autopilot.transports.base import InboundMessage, load_offset
from autopilot.transports.max import MaxTransport
from autopilot.transports.telegram import TelegramTransport
from autopilot.vault import Vault

SECRET = "Str0ngP@ssw0rd-canary"


class FakeTransport:
    """Отдаёт заранее заданный список сообщений и запоминает отправленное."""

    def __init__(self, name="telegram", messages=None, impersonation=True):
        self.name = name
        self.messages = list(messages or [])
        self.sent: list[tuple[str, str, str | None]] = []
        self._impersonation = impersonation

    def supports_impersonation(self) -> bool:
        return self._impersonation

    async def poll(self):
        for m in self.messages:
            yield m

    async def send(self, chat_ref, text, connection_id=None) -> str:
        self.sent.append((chat_ref, text, connection_id))
        return f"sent-{len(self.sent)}"

    async def resolve_chat(self, handle):
        return None


class QuietComm:
    """Communicator-заглушка: считает вызовы, ничего не шлёт."""

    def __init__(self):
        self.forwarded: list[str] = []
        self.owner_notes: list[str] = []
        self.acks: list[tuple[int, str]] = []

    async def notify_owner(self, text):
        self.owner_notes.append(text)

    async def confirm_access_received(self, project, transport=None, chat_id=None):
        self.acks.append((project.id, chat_id))

    async def __call__(self, *a, **kw):         # pragma: no cover
        pass


class Comm(QuietComm):
    async def incoming(self, project, text, transport=None, chat_id=None):
        self.forwarded.append(text)


def tg_update(update_id: int, chat_id: int, message_id: int, text: str,
              *, username="ivan", key="business_message", edited=False):
    msg = {
        "message_id": message_id,
        "date": 1770000000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 555, "username": username, "first_name": "Иван"},
        "text": text,
        "business_connection_id": "bizconn-1",
    }
    if edited:
        msg["edit_date"] = 1770000100
    return {"update_id": update_id, key: msg}


def to_inbound(update: dict) -> InboundMessage:
    """Прогоняем фикстуру через настоящий нормализатор транспорта."""
    tr = TelegramTransport(token="x", client=object())
    key = "edited_business_message" if "edited_business_message" in update else \
        ("business_message" if "business_message" in update else "message")
    return tr._normalize(update[key], cursor=str(update["update_id"]),
                         edited=key.startswith("edited"))


async def count_messages() -> int:
    async with Session() as s:
        return (await s.execute(select(func.count()).select_from(ChatMessage))).scalar_one()


# ---------- offset и дедуп ----------

async def test_offset_persistence(db):
    """Рестарт не теряет и не дублирует: offset живёт в БД."""
    p = await make_project(tg_chat_id="100")
    ing = Ingest([FakeTransport()], Comm())
    tr = FakeTransport()

    await ing.handle(tr, to_inbound(tg_update(10, 100, 1, "первое")))
    await ing.handle(tr, to_inbound(tg_update(11, 100, 2, "второе")))
    assert await load_offset("telegram") == "11"

    # «рестарт»: новый объект Ingest, состояние только из БД
    ing2 = Ingest([FakeTransport()], Comm())
    assert await load_offset("telegram") == "11"
    await ing2.handle(tr, to_inbound(tg_update(12, 100, 3, "третье")))

    assert await count_messages() == 3
    assert await load_offset("telegram") == "12"

    async with Session() as s:
        rows = (await s.execute(select(ChatMessage.text).order_by(ChatMessage.id))).scalars().all()
    assert rows == ["первое", "второе", "третье"]
    assert p.id is not None


async def test_dedup_on_reconnect(db):
    """Повторный апдейт после переподключения не создаёт вторую строку."""
    await make_project(tg_chat_id="100")
    ing = Ingest([], Comm())
    tr = FakeTransport()

    upd = tg_update(10, 100, 1, "привет")
    await ing.handle(tr, to_inbound(upd))
    await ing.handle(tr, to_inbound(upd))      # тот же update_id
    await ing.handle(tr, to_inbound(upd))

    assert await count_messages() == 1


async def test_cross_transport_ids(db):
    """Одинаковые message_id в разных мессенджерах — не дубль."""
    p = await make_project(tg_chat_id="100")
    await add_chat(p.id, "max", "100")
    ing = Ingest([], Comm())

    await ing.handle(FakeTransport("telegram"),
                     InboundMessage(transport="telegram", chat_id="100",
                                    message_id="777", text="из телеграма", cursor="1"))
    await ing.handle(FakeTransport("max"),
                     InboundMessage(transport="max", chat_id="100",
                                    message_id="777", text="из макса", cursor="1"))

    assert await count_messages() == 2
    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage.transport, ChatMessage.text).order_by(ChatMessage.id))).all()
    assert rows == [("telegram", "из телеграма"), ("max", "из макса")]


async def test_edit_and_delete(db):
    """Правка и удаление отражаются, оригинал не затирается."""
    await make_project(tg_chat_id="100")
    ing = Ingest([], Comm())
    tr = FakeTransport()

    await ing.handle(tr, to_inbound(tg_update(10, 100, 1, "было так")))
    await ing.handle(tr, to_inbound(
        tg_update(11, 100, 1, "стало иначе", key="edited_business_message", edited=True)))

    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()
    assert row.text == "стало иначе"
    assert row.edited_at is not None
    assert row.raw_json["revisions"][0]["text"] == "было так", "исходный текст потерян"

    await ing.handle(tr, InboundMessage(transport="telegram", chat_id="100",
                                        message_id="1", deleted=True, cursor="12"))
    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()
    assert row.deleted is True
    assert row.text == "стало иначе", "удаление у клиента не должно стирать нашу историю"
    assert await count_messages() == 1


# ---------- перехват секретов ----------

@pytest.fixture
def live_vault(tmp_path, monkeypatch):
    v = Vault(path=tmp_path / "secrets.enc", key=Fernet.generate_key())
    monkeypatch.setattr("autopilot.secrets_scan.default_vault", v)
    monkeypatch.setattr("autopilot.vault.vault", v)
    return v


async def test_secret_intercept(db, live_vault, caplog):
    """ftp://user:pass@host сохраняется с плейсхолдером; пароля нет ни в БД, ни в логах."""
    p = await make_project(tg_chat_id="100")
    comm = Comm()
    ing = Ingest([], comm)

    with caplog.at_level("DEBUG"):
        await ing.handle(FakeTransport(), to_inbound(
            tg_update(10, 100, 1, f"держи доступ ftp://ivan:{SECRET}@ftp.example.kz")))

    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()

    assert SECRET not in row.text, "пароль сохранён в открытом виде"
    assert "{{SECRET:" in row.text
    assert SECRET not in str(row.raw_json), "пароль осел в raw_json"

    name = row.text.split("{{SECRET:")[1].split("}}")[0]
    assert live_vault.get(name) == SECRET, "значение не достаётся из vault"

    assert SECRET not in caplog.text, "пароль утёк в лог"
    assert comm.acks == [(p.id, "100")], "клиенту не подтвердили получение доступа"
    assert comm.forwarded == [], "текст с секретом не должен уходить пересылкой"


async def test_secret_intercept_freeform(db, live_vault):
    """«логин admin пароль Qw3rty!» — тот же перехват на свободном тексте."""
    await make_project(tg_chat_id="100")
    ing = Ingest([], Comm())

    await ing.handle(FakeTransport(), to_inbound(
        tg_update(10, 100, 1, "логин admin пароль Qw3rty!2026 от панели")))

    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()
    assert "Qw3rty!2026" not in row.text
    assert "{{SECRET:" in row.text
    assert "Qw3rty!2026" in set(live_vault.get(n) for n in live_vault.names())


# ---------- привязка чата ----------

async def test_unbound_chat(db):
    """Сообщение из непривязанного чата сохранено, владелец уведомлён."""
    await make_project(tg_chat_id="200", title="открытый проект")
    comm = Comm()
    ing = Ingest([], comm)

    await ing.handle(FakeTransport(), to_inbound(
        tg_update(10, 999, 1, "здравствуйте, я по поводу сайта", username="stranger")))

    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()
    assert row.chat_id == "999"
    assert row.project_id is None, "чужой чат не должен приклеиваться к проекту наугад"
    assert row.text == "здравствуйте, я по поводу сайта", "сообщение потеряно"

    assert len(comm.owner_notes) == 1
    note = comm.owner_notes[0]
    assert "999" in note and "открытый проект" in note and "/bind" in note


async def test_bind_by_username(db):
    """@username из колонки менеджера превращается в chat_id по первому сообщению."""
    async with Session() as s:
        p = Project(client="к", title="сайт", chat_ref="tg:@ivan", ready_for_work=True)
        s.add(p)
        await s.commit()

    ing = Ingest([], Comm())
    await ing.handle(FakeTransport(), to_inbound(
        tg_update(10, 4242, 1, "добрый день", username="ivan")))

    async with Session() as s:
        chat = (await s.execute(select(ProjectChat))).scalars().one()
        row = (await s.execute(select(ChatMessage))).scalars().one()
        proj = await s.get(Project, p.id)
        tasks = (await s.execute(select(Task).where(Task.lane == "chat"))).scalars().all()

    assert chat.project_id == p.id
    assert chat.chat_id == "4242"
    assert chat.transport == "telegram"
    assert row.project_id == p.id
    assert proj.client_replied_at is not None, "признак «клиент ответил» не выставлен"
    assert len(tasks) == 1, "входящее должно поставить слот в полосе chat"


async def test_manual_bind_adopts_orphans(db):
    """Ручная привязка подбирает уже сохранённые осиротевшие сообщения."""
    p = await make_project(tg_chat_id="200")
    ing = Ingest([], Comm())
    await ing.handle(FakeTransport(), to_inbound(
        tg_update(10, 999, 1, "это я", username="stranger")))

    assert await ing.bind_chat(p.id, "telegram", "999") is True

    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage).where(ChatMessage.chat_id == "999"))).scalars().all()
    assert [r.project_id for r in rows] == [p.id]


def test_parse_chat_ref():
    assert parse_chat_ref("tg:@ivan") == ("telegram", "@ivan")
    assert parse_chat_ref("max:@ivan") == ("max", "@ivan")
    assert parse_chat_ref("@ivan") == ("telegram", "@ivan")      # старые строки не ломаем
    assert parse_chat_ref("123456") == ("telegram", "123456")
    assert parse_chat_ref("") is None


# ---------- нормализация между мессенджерами ----------

def test_transport_normalization():
    """Апдейт MAX и апдейт Telegram дают одинаковую по форме InboundMessage."""
    tg = to_inbound(tg_update(1, 100, 55, "привет"))

    mx = MaxTransport(token="x", client=object()).normalize({
        "update_type": "message_created",
        "timestamp": 1770000000000,
        "marker": 981,
        "message": {
            "sender": {"user_id": 555, "username": "ivan"},
            "recipient": {"chat_id": 100, "chat_type": "dialog"},
            "body": {"mid": "mid-55", "seq": 7, "text": "привет"},
        },
    })

    assert {f for f in tg.__dataclass_fields__} == {f for f in mx.__dataclass_fields__}
    for field in ("text", "chat_id", "sender_id", "handle", "has_media", "deleted", "edited"):
        assert getattr(tg, field) == getattr(mx, field), f"поле {field} разошлось"
    assert tg.transport == "telegram" and mx.transport == "max"
    assert tg.message_id == "55" and mx.message_id == "mid-55"
    assert isinstance(tg.date, dt.datetime) and isinstance(mx.date, dt.datetime)
    assert tg.date.tzinfo is not None and mx.date.tzinfo is not None
    # бизнес-режим есть только в Telegram
    assert tg.connection_id == "bizconn-1"
    assert mx.connection_id is None


def test_max_normalizes_by_update_type():
    """В MAX тип события берётся из update_type, а не по наличию поля."""
    tr = MaxTransport(token="x", client=object())
    assert tr.normalize({"update_type": "bot_added", "chat_id": 1}) is None
    removed = tr.normalize({"update_type": "message_removed", "chat_id": 5,
                            "message_id": "mid-9", "timestamp": 1770000000000})
    assert removed.deleted is True and removed.message_id == "mid-9"


def test_max_transport_has_no_impersonation():
    assert MaxTransport(token="x", client=object()).supports_impersonation() is False
    assert TelegramTransport(token="x", client=object()).supports_impersonation() is True


# ---------- offset хранится, а не выдумывается ----------

async def test_offset_row_is_single(db):
    ing = Ingest([], Comm())
    await make_project(tg_chat_id="100")
    for i in range(3):
        await ing.handle(FakeTransport(), to_inbound(tg_update(20 + i, 100, i + 1, f"m{i}")))
    async with Session() as s:
        rows = (await s.execute(select(TransportState))).scalars().all()
    assert len(rows) == 1 and rows[0].offset == "22"
    assert cfg.tg_poll_timeout > 0
