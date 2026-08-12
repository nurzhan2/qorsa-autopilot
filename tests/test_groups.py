"""Групповые чаты: опознание по названию и молчание в чужом разговоре."""
from __future__ import annotations

from conftest import make_project
from sqlalchemy import func, select

from autopilot import roles
from autopilot.config import cfg
from autopilot.db import ChatMessage, Project, ProjectChat, Session, Task
from autopilot.groups import match_project, parse_title, similarity
from autopilot.ingest import Ingest
from autopilot.transports.base import InboundMessage
from autopilot.transports.telegram import TelegramTransport

GROUP = "-1001234567890"


class Comm:
    def __init__(self):
        self.forwarded: list[tuple[str, bool]] = []
        self.owner_notes: list[str] = []
        self.acks: list = []

    async def incoming(self, project, text, transport=None, chat_id=None, in_group=False):
        self.forwarded.append((text, in_group))

    async def notify_owner(self, text):
        self.owner_notes.append(text)

    async def confirm_access_received(self, project, transport=None, chat_id=None):
        self.acks.append(chat_id)


class Tr:
    name = "telegram"

    def supports_impersonation(self): return True
    async def send(self, chat_ref, text, connection_id=None): return "1"
    async def resolve_chat(self, handle): return None


def group_update(update_id: int, message_id: int, text: str, sender_id: int,
                 *, title="Qorsa • Айгерим • интернет-магазин", reply_to_bot=False,
                 username="ivan"):
    msg = {
        "message_id": message_id,
        "date": 1780000000,
        "chat": {"id": int(GROUP), "type": "supergroup", "title": title},
        "from": {"id": sender_id, "username": username, "first_name": "Кто-то"},
        "text": text,
    }
    if reply_to_bot:
        msg["reply_to_message"] = {"message_id": 1, "from": {"id": 999}}
    return {"update_id": update_id, "message": msg}


def inbound(update: dict) -> InboundMessage:
    tr = TelegramTransport(token="x", client=object())
    return tr._normalize(update["message"], cursor=str(update["update_id"]), edited=False)


# ---------- опознание группы ----------

def test_parse_title():
    assert parse_title("Qorsa • Айгерим • интернет-магазин") == ("Айгерим", "интернет-магазин")
    assert parse_title("Qorsa • Ерлан • лендинг под запуск") == ("Ерлан", "лендинг под запуск")
    assert parse_title("просто чат") is None


def test_similarity_is_forgiving_to_typing():
    assert similarity("интернет-магазин", "интернет магазин") > 0.9
    assert similarity("Айгерим", "айгерим") == 1.0
    assert similarity("лендинг", "интернет-магазин") < 0.5


async def test_group_bind_by_name(db, monkeypatch):
    """Группа «Qorsa • Айгерим • интернет-магазин» привязывается к нужной строке."""
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    target = await make_project(title="интернет-магазин", tg_chat_id=None)
    async with Session() as s:
        p = await s.get(Project, target.id)
        p.client = "Айгерим"
        await s.commit()
    other = await make_project(title="лендинг под запуск", tg_chat_id=None)
    async with Session() as s:
        p = await s.get(Project, other.id)
        p.client = "Ерлан"
        await s.commit()

    comm = Comm()
    ing = Ingest([], comm)
    await ing.handle(Tr(), inbound(group_update(1, 10, "добрый день!", 111)))

    async with Session() as s:
        chat = (await s.execute(select(ProjectChat))).scalars().one()
        row = (await s.execute(select(ChatMessage))).scalars().one()
    assert chat.project_id == target.id, "группа привязалась не к тому проекту"
    assert chat.is_group is True
    assert chat.handle == "Qorsa • Айгерим • интернет-магазин"
    assert row.project_id == target.id
    assert comm.owner_notes == [], "уверенное совпадение не должно требовать подтверждения"


async def test_group_unclear_name_asks_owner(db):
    """Название не по шаблону — как раньше: уведомление владельцу и /bind."""
    await make_project(title="интернет-магазин", tg_chat_id=None)
    comm = Comm()
    ing = Ingest([], comm)

    await ing.handle(Tr(), inbound(group_update(1, 10, "привет", 111, title="рабочая группа")))

    async with Session() as s:
        row = (await s.execute(select(ChatMessage))).scalars().one()
        n = (await s.execute(select(func.count()).select_from(ProjectChat))).scalar_one()
    assert row.project_id is None
    assert n == 0
    assert len(comm.owner_notes) == 1 and "/bind" in comm.owner_notes[0]
    assert row.text == "привет", "сообщение из неопознанной группы потеряно"


async def test_group_ambiguous_name_is_not_guessed(db):
    """Два похожих проекта — привязку не угадываем."""
    for title in ("сайт визитка", "сайт-визитка"):
        p = await make_project(title=title, tg_chat_id=None)
        async with Session() as s:
            row = await s.get(Project, p.id)
            row.client = "Дана"
            await s.commit()

    project_id, score, why = await match_project("Qorsa • Дана • сайт визитка")
    assert project_id is None
    assert "неоднозначно" in why


# ---------- поведение в группе ----------

async def test_bot_silent_in_dialogue(db, monkeypatch):
    """Обмен репликами менеджера и клиента не вызывает ответа бота."""
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    p = await make_project(title="интернет-магазин", tg_chat_id=None)
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.client = "Айгерим"
        await s.commit()

    comm = Comm()
    ing = Ingest([], comm)

    dialogue = [
        (1, 10, "сколько будет стоить доработка?", 111),       # клиент про деньги
        (2, 11, "давайте посчитаем, вернусь с цифрой", 777),   # я (владелец-менеджер)
        (3, 12, "хорошо, жду", 111),                            # клиент
        (4, 13, "нужен ещё блок отзывов", 111),                 # требование, но не к боту
    ]
    for upd in dialogue:
        await ing.handle(Tr(), inbound(group_update(*upd)))

    async with Session() as s:
        stored = (await s.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
        tasks = (await s.execute(select(Task).where(Task.lane == "chat"))).scalars().all()

    assert len(stored) == 4, "бот обязан читать и запоминать всё"
    assert [m.sender_role for m in stored] == ["client", "owner", "client", "client"]
    assert comm.forwarded == [], "бот влез в разговор менеджера с клиентом"
    assert tasks == [], "молчание не должно занимать слот полосы chat"


async def test_bot_replies_on_mention(db, monkeypatch):
    """Прямое обращение вызывает ответ."""
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    monkeypatch.setattr(cfg, "bot_username", "qorsa_bot")
    p = await make_project(title="интернет-магазин", tg_chat_id=None)
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.client = "Айгерим"
        await s.commit()

    comm = Comm()
    ing = Ingest([], comm)

    await ing.handle(Tr(), inbound(group_update(1, 10, "просто болтаем", 111)))
    assert comm.forwarded == []

    await ing.handle(Tr(), inbound(
        group_update(2, 11, "@qorsa_bot ftp не пускает, посмотри", 111)))
    assert len(comm.forwarded) == 1
    text, in_group = comm.forwarded[0]
    assert "ftp не пускает" in text and in_group is True

    # ответ на сообщение бота — тоже обращение
    await ing.handle(Tr(), inbound(
        group_update(3, 12, "да, вот так", 111, reply_to_bot=True)))
    assert len(comm.forwarded) == 2

    async with Session() as s:
        tasks = (await s.execute(select(Task).where(Task.lane == "chat"))).scalars().all()
    assert len(tasks) == 2, "обращение к боту должно ставить слот полосы chat"


async def test_manager_message_never_triggers_bot(db, monkeypatch):
    """Даже прямое упоминание от менеджера не считается запросом клиента."""
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    monkeypatch.setattr(cfg, "bot_username", "qorsa_bot")
    p = await make_project(title="интернет-магазин", tg_chat_id=None)
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.client = "Айгерим"
        await s.commit()

    comm = Comm()
    ing = Ingest([], comm)
    await ing.handle(Tr(), inbound(group_update(1, 10, "@qorsa_bot сделай отчёт", 777)))

    async with Session() as s:
        stored = (await s.execute(select(ChatMessage))).scalars().one()
    assert stored.sender_role == roles.OWNER
    assert comm.forwarded == []


async def test_group_commercial_question_is_silent(db, monkeypatch):
    """Вопрос про деньги в группе не пересылается: человек сидит в той же группе."""
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    monkeypatch.setattr(cfg, "bot_username", "qorsa_bot")
    from autopilot.communicator import Communicator

    p = await make_project()
    comm = Communicator()
    assert await comm.incoming(p, "сколько будет стоить?", "telegram", GROUP,
                               in_group=True) is None
    # в личке поведение прежнее — уведомление уходит человеку
    m = await comm.incoming(p, "сколько будет стоить?", "telegram", "42", in_group=False)
    assert m is not None and m.route == "to_owner"
