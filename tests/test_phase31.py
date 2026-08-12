"""Фаза 3.1: один аккаунт, подтверждённые предложения, стойкость импорта."""
from __future__ import annotations

import json

import pytest
from conftest import make_project
from sqlalchemy import select
from test_brief import CHAT, FakeAnthropic, add_msg, ev, reply

from autopilot import roles
from autopilot.brief import (ORIGIN_CLIENT, ORIGIN_CONFIRMED, Brief, agreement,
                             find_confirmations, is_bare_agreement)
from autopilot.communicator import (TO_CLIENT, TO_OWNER, TOPIC_COMMERCIAL, Communicator,
                                    commercial_route, route, topic)
from autopilot.config import cfg
from autopilot.db import ChatMessage, ChatParticipant, Project, ProjectChat, Session
from autopilot.ingest import Ingest
from autopilot.transports.telegram import TelegramTransport


async def _reload(p: Project) -> Project:
    async with Session() as s:
        return await s.get(Project, p.id)


def texts(data: dict, field: str = "deliverables") -> list[str]:
    return [d["text"] for d in data.get(field) or []]


# ---------- один аккаунт ----------

async def test_manager_role_absent(db, monkeypatch):
    """Сообщения с owner-id получают роль owner, роль manager не назначается."""
    monkeypatch.setattr(cfg, "owner_tg_id", "777")
    monkeypatch.setattr(cfg, "manager_tg_id", "888")   # алиас, не отдельная роль
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    monkeypatch.setattr(cfg, "manager_separate", False)

    assert roles.role_of("telegram", "777") == roles.OWNER
    assert roles.role_of("telegram", "888") == roles.OWNER, "менеджер должен быть алиасом владельца"
    assert roles.role_of("telegram", "999") == roles.BOT
    assert roles.role_of("telegram", "123") == roles.CLIENT

    for sender in ("777", "888", "999", "123"):
        await roles.remember("telegram", CHAT, sender, "кто-то")

    async with Session() as s:
        assigned = {p.role for p in (await s.execute(select(ChatParticipant))).scalars()}
    assert roles.MANAGER not in assigned, "роль manager не должна быть занята никем"
    assert assigned == {roles.OWNER, roles.BOT, roles.CLIENT}

    # но механизм цел: менеджер может отделиться
    monkeypatch.setattr(cfg, "manager_separate", True)
    assert roles.role_of("telegram", "888") == roles.MANAGER
    assert roles.MANAGER in roles.ROLES


def test_commercial_route_collapses(monkeypatch):
    """to_manager схлопнулся в to_owner, но тема осталась коммерческой."""
    monkeypatch.setattr(cfg, "manager_separate", False)
    assert commercial_route() == TO_OWNER
    assert topic("сколько будет стоить?") == TOPIC_COMMERCIAL
    assert route("сколько будет стоить?") == TO_OWNER

    monkeypatch.setattr(cfg, "manager_separate", True)
    assert commercial_route() == "to_manager"


async def test_commercial_silence_survives_collapse(db, monkeypatch):
    """Схлопывание маршрута не должно превратить молчание в ответ."""
    monkeypatch.setattr(cfg, "manager_separate", False)
    p = await make_project()
    comm = Communicator()

    # в группе про деньги бот молчит, хотя адресат теперь владелец
    assert await comm.incoming(p, "сколько будет стоить доработка?", "telegram", CHAT,
                               in_group=True) is None
    assert await comm.incoming(p, "а когда будет готово?", "telegram", CHAT,
                               in_group=True) is None

    # в личке по-прежнему уведомляем
    m = await comm.incoming(p, "сколько будет стоить?", "telegram", "42", in_group=False)
    assert m is not None and m.route == TO_OWNER

    # техника в группе к боту доходит
    tech = await comm.incoming(p, "ftp не пускает, пароль не подходит", "telegram", CHAT,
                               in_group=True)
    assert tech is not None and tech.route == TO_OWNER


async def test_missing_owner_id_fails_fast(db, monkeypatch):
    """Пустой OWNER_TG_ID не даёт стартовать ingest."""
    monkeypatch.setattr(cfg, "owner_tg_id", "")
    monkeypatch.setattr(cfg, "owner_max_id", "")
    assert roles.owner_configured() is False

    ing = Ingest([], _Comm())
    with pytest.raises(RuntimeError, match="OWNER_TG_ID"):
        await ing.run()

    monkeypatch.setattr(cfg, "owner_tg_id", "777")
    assert roles.owner_configured() is True
    await ing.run()          # транспортов нет — просто выходит, но уже без падения


# ---------- подтверждённое предложение ----------

async def test_confirmed_proposal(db):
    """Предложение владельца + «да, давайте» клиента → пункт с origin."""
    p = await make_project()
    await add_msg(p.id, "1", "хочу интернет-магазин", roles.CLIENT)
    await add_msg(p.id, "2", "давайте сделаем фильтр по бренду и по типу кожи", roles.OWNER)
    await add_msg(p.id, "3", "да, давайте", roles.CLIENT)

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "фильтр по бренду и типу кожи", "evidence": ev("2", "3")}]))
    data = await Brief(client=fake).build(p)

    assert texts(data) == ["фильтр по бренду и типу кожи"]
    item = data["deliverables"][0]
    assert item["origin"] == ORIGIN_CONFIRMED
    assert item["evidence"] == [f"telegram:{CHAT}:2", f"telegram:{CHAT}:3"]


async def test_confirmed_proposal_by_reply(db):
    """Reply на предложение связывает их даже через много сообщений."""
    p = await make_project()
    await add_msg(p.id, "1", "давайте добавим форму обратной связи", roles.OWNER)
    for i in range(2, 8):
        await add_msg(p.id, str(i), f"обсуждаем что-то другое {i}", roles.CLIENT)
    async with Session() as s:
        m = ChatMessage(transport="telegram", chat_id=CHAT, tg_message_id="9",
                        project_id=p.id, sender_role=roles.CLIENT, sender_id="1",
                        text="да, хорошо", reply_to="1")
        s.add(m)
        await s.commit()

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "форма обратной связи", "evidence": ev("1")}]))
    data = await Brief(client=fake).build(p)
    assert texts(data) == ["форма обратной связи"]
    assert data["deliverables"][0]["origin"] == ORIGIN_CONFIRMED


async def test_rejected_proposal(db):
    """Предложение + «нет, не надо» → пункт в out_of_scope, не в требованиях."""
    p = await make_project()
    await add_msg(p.id, "1", "хочу интернет-магазин", roles.CLIENT)
    await add_msg(p.id, "2", "давайте сразу прикрутим программу лояльности", roles.OWNER)
    await add_msg(p.id, "3", "нет, не надо, потом", roles.CLIENT)

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "магазин", "evidence": ev("1")},
        {"text": "программа лояльности", "evidence": ev("2", "3")}]))
    data = await Brief(client=fake).build(p)

    assert texts(data) == ["магазин"]
    out = texts(data, "out_of_scope")
    assert "программа лояльности" in out, "отказ не должен теряться молча"
    rejected = [i for i in data["out_of_scope"] if i.get("rejected")]
    assert rejected and rejected[0]["origin"] == ORIGIN_CONFIRMED


async def test_bare_agreement_invalid(db):
    """«да, хорошо» без предшествующего предложения → пункт отбрасывается."""
    p = await make_project()
    await add_msg(p.id, "1", "здравствуйте", roles.CLIENT)
    await add_msg(p.id, "2", "да, хорошо", roles.CLIENT)

    fake = FakeAnthropic(reply(goal="магазин", goal_ev=("2",), deliverables=[
        {"text": "интеграция с 1С", "evidence": ev("2")}]))
    data = await Brief(client=fake).build(p)

    assert texts(data) == []
    assert data["goal"] is None
    meta = (await _reload(p)).brief["_meta"]
    assert any("голое согласие" in d for d in meta["dropped"])


async def test_confirm_window(db, monkeypatch):
    """Согласие через 5 сообщений при окне 3 не засчитывается."""
    monkeypatch.setattr(cfg, "confirm_window", 3)
    p = await make_project()
    await add_msg(p.id, "1", "давайте сделаем блок отзывов", roles.OWNER)
    for i in range(2, 6):
        await add_msg(p.id, str(i), f"а вот ещё вопрос номер {i}", roles.CLIENT)
    await add_msg(p.id, "6", "да, давайте", roles.CLIENT)

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "блок отзывов", "evidence": ev("1", "6")}]))
    data = await Brief(client=fake).build(p)

    assert texts(data) == [], "согласие вне окна не должно подтверждать предложение"

    # В пределах окна — засчитывается. Между предложением и согласием стоит
    # реплика бота: она связь не рвёт, в отличие от реплики клиента на другую
    # тему, — иначе любое «секунду, уточняю» ломало бы подтверждение
    p2 = await make_project(title="второй")
    await add_msg(p2.id, "11", "давайте сделаем блок отзывов", roles.OWNER)
    await add_msg(p2.id, "12", "принял, записываю", roles.BOT)
    await add_msg(p2.id, "13", "да, давайте", roles.CLIENT)
    fake2 = FakeAnthropic(reply(deliverables=[
        {"text": "блок отзывов", "evidence": [f"telegram:{CHAT}:11", f"telegram:{CHAT}:13"]}]))
    data2 = await Brief(client=fake2).build(p2)
    assert texts(data2) == ["блок отзывов"]
    assert data2["deliverables"][0]["origin"] == ORIGIN_CONFIRMED


async def test_client_topic_change_breaks_link(db):
    """Клиент вклинился с другой темой — связь предложения и согласия рвётся."""
    p = await make_project()
    await add_msg(p.id, "1", "давайте сделаем калькулятор доставки", roles.OWNER)
    await add_msg(p.id, "2", "кстати, а логотип можно поменять?", roles.CLIENT)
    await add_msg(p.id, "3", "да", roles.CLIENT)

    async with Session() as s:
        messages = (await s.execute(
            select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    assert find_confirmations(messages, window=3) == {}


def test_agreement_markers():
    """«нужен сайт» — это требование, а не согласие."""
    assert agreement("да, давайте") == "yes"
    assert agreement("нет, не надо") == "no"
    assert agreement("нужно") == "yes"
    assert agreement("нужен сайт") is None, "требование не должно считаться согласием"
    assert agreement("а сколько стоит?") is None
    assert is_bare_agreement("ок") is True
    assert is_bare_agreement("да, и ещё нужен блок отзывов") is False


async def test_client_words_still_win(db):
    """Обычный путь не сломался: слова клиента дают origin=client."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен каталог с фильтрами", roles.CLIENT)
    fake = FakeAnthropic(reply(deliverables=[
        {"text": "каталог с фильтрами", "evidence": ev("1")}]))
    data = await Brief(client=fake).build(p)
    assert data["deliverables"][0]["origin"] == ORIGIN_CLIENT


# ---------- переименование группы ----------

async def test_group_rename(db, monkeypatch):
    """Переименование группы не рвёт привязку."""
    monkeypatch.setattr(cfg, "bot_tg_id", "999")
    monkeypatch.setattr(cfg, "owner_tg_id", "777")
    target = await make_project(title="интернет-магазин", tg_chat_id=None)
    async with Session() as s:
        row = await s.get(Project, target.id)
        row.client = "Айгерим"
        await s.commit()

    comm = _Comm()
    ing = Ingest([], comm)
    tr = TelegramTransport(token="x", client=object())

    def upd(update_id, mid, text, title):
        return tr._normalize({
            "message_id": mid, "date": 1780000000,
            "chat": {"id": -1001, "type": "supergroup", "title": title},
            "from": {"id": 111, "username": "ivan", "first_name": "Иван"},
            "text": text}, cursor=str(update_id), edited=False)

    await ing.handle(tr, upd(1, 10, "привет", "Qorsa • Айгерим • интернет-магазин"))
    async with Session() as s:
        chat = (await s.execute(select(ProjectChat))).scalars().one()
    assert chat.project_id == target.id

    # менеджер переименовал группу во что-то своё
    await ing.handle(tr, upd(2, 11, "ещё сообщение", "магазин Айгерим (рабочая)"))

    async with Session() as s:
        chats = (await s.execute(select(ProjectChat))).scalars().all()
        rows = (await s.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    assert len(chats) == 1, "переименование создало вторую привязку"
    assert chats[0].project_id == target.id, "привязка порвалась при переименовании"
    assert chats[0].handle == "магазин Айгерим (рабочая)", "название не обновилось"
    assert [r.project_id for r in rows] == [target.id, target.id]
    assert comm.owner_notes, "расхождение названия должно быть показано предупреждением"


# ---------- импорт реальных форм ----------

async def test_import_real_shapes(db, tmp_path):
    """Пересылки, реакции, голосовые, кружки, опросы, сервисные сообщения."""
    import import_helper

    export = {
        "name": "Клиент", "type": "personal_chat", "id": 111222333,
        "messages": [
            {"id": 1, "type": "service", "date_unixtime": "1784023331",
             "actor": "Иван", "actor_id": "user111222333",
             "action": "join_group_by_link"},
            {"id": 2, "type": "message", "date_unixtime": "1784023420",
             "from": "Иван", "from_id": "user111222333",
             "text": "нужен интернет-магазин"},
            {"id": 3, "type": "message", "date_unixtime": "1784023500",
             "from": "Я", "from_id": "user777000777",
             "text": [{"type": "link", "text": "https://example.kz"}, " — вот пример"],
             "reactions": [{"emoji": "👍", "count": 1}]},
            {"id": 4, "type": "message", "date_unixtime": "1784023600",
             "from": "Иван", "from_id": "user111222333",
             "forwarded_from": "Какой-то канал", "text": "пересланное сообщение"},
            {"id": 5, "type": "message", "date_unixtime": "1784023700",
             "from": "Иван", "from_id": "user111222333",
             "file": "(File not included. Change data exporting settings to download.)",
             "media_type": "voice_message", "duration_seconds": 7, "text": ""},
            {"id": 6, "type": "message", "date_unixtime": "1784023800",
             "from": "Иван", "from_id": "user111222333",
             "file": "(File not included...)", "media_type": "video_message", "text": ""},
            {"id": 7, "type": "message", "date_unixtime": "1784023900",
             "from": "Иван", "from_id": "user111222333",
             "poll": {"question": "какой вариант лучше?",
                      "closed": False, "total_voters": 2,
                      "answers": [{"text": "первый", "voters": 1},
                                  {"text": "второй", "voters": 1}]},
             "text": ""},
            {"id": 8, "type": "message", "date_unixtime": "1784024000",
             "from": "Иван", "from_id": "user111222333",
             "reply_to_message_id": 2, "text": "и ещё вот это"},
            {"id": 9, "type": "message", "date_unixtime": "1784024100",
             "from": "Иван", "from_id": "user111222333",
             "sticker_emoji": "🔥", "media_type": "sticker", "text": ""},
            {"id": 10, "type": "service", "date_unixtime": "1784024200",
             "actor": "Я", "actor_id": "user777000777", "action": "pin_message"},
            {"type": "message", "text": "без id — не должно ронять импорт"},
        ],
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")

    p = await make_project(tg_chat_id=None)
    assert await import_helper.run_import(path, p.id) == 0

    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage).order_by(ChatMessage.tg_message_id))).scalars().all()
    by_id = {r.tg_message_id: r for r in rows}

    # сервисные и битые пропущены, нумерация исходных id сохранена
    assert set(by_id) == {"2", "3", "4", "5", "6", "7", "8", "9"}

    # роли личного чата: собеседник — клиент, остальные — владелец
    assert by_id["2"].sender_role == roles.CLIENT
    assert by_id["3"].sender_role == roles.OWNER

    # текст-список склеен
    assert by_id["3"].text == "https://example.kz — вот пример"
    # медиа опознано и не притворяется текстом
    assert (by_id["5"].has_media, by_id["5"].media_kind) == (True, "voice")
    assert (by_id["6"].has_media, by_id["6"].media_kind) == (True, "video_note")
    assert (by_id["9"].has_media, by_id["9"].media_kind) == (True, "sticker")
    # опрос: вопрос сохранён, варианты не выдуманы
    assert by_id["7"].has_media is True and by_id["7"].media_kind == "poll"
    assert "какой вариант лучше?" in by_id["7"].text
    # ответ на сообщение
    assert by_id["8"].reply_to == "2"
    # пересылка помечена: это чужие слова
    assert by_id["4"].raw_json.get("forwarded_from") == "Какой-то канал"
    assert by_id["3"].raw_json.get("reactions") == 1

    # повторный импорт не плодит дублей
    assert await import_helper.run_import(path, p.id) == 0
    async with Session() as s:
        again = (await s.execute(select(ChatMessage))).scalars().all()
    assert len(again) == len(rows)


class _Comm:
    def __init__(self):
        self.owner_notes: list[str] = []
        self.forwarded: list[str] = []

    async def incoming(self, project, text, transport=None, chat_id=None, in_group=False):
        self.forwarded.append(text)

    async def notify_owner(self, text):
        self.owner_notes.append(text)

    async def confirm_access_received(self, project, transport=None, chat_id=None):
        return None


async def test_polite_opening_does_not_confirm(db):
    """«Хорошо. Сайт хочу на wordpress» — это требование клиента, а не
    подтверждение того, что мы предложили до этого.

    Регрессия: вежливое начало реплики задним числом утверждало предыдущее
    предложение владельца. Поймано на живом чате в brief_eval.
    """
    p = await make_project()
    await add_msg(p.id, "1", "предлагаю сделать личный кабинет с историей заказов",
                  roles.OWNER)
    await add_msg(p.id, "2", "Хорошо. Сайт хочу на wordpress, так удобнее", roles.CLIENT)

    async with Session() as s:
        messages = (await s.execute(
            select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    assert find_confirmations(messages) == {}, "вежливое «хорошо» подтвердило предложение"

    # ссылка только на предложение владельца — пункт выбрасывается:
    # подтверждения нет, а своих слов клиента в evidence не указано
    fake = FakeAnthropic(reply(
        goal="сайт на wordpress", goal_ev=("2",),
        deliverables=[{"text": "личный кабинет с историей заказов", "evidence": ev("1")}]))
    data = await Brief(client=fake).build(p)
    assert texts(data) == []

    # а вот голое «да» рядом — подтверждает
    p2 = await make_project(title="второй")
    await add_msg(p2.id, "11", "предлагаю сделать личный кабинет", roles.OWNER)
    await add_msg(p2.id, "12", "да, давайте", roles.CLIENT)
    fake2 = FakeAnthropic(reply(
        goal="кабинет", goal_ev=("11", "12"),
        deliverables=[{"text": "личный кабинет",
                       "evidence": [f"telegram:{CHAT}:11", f"telegram:{CHAT}:12"]}]))
    data2 = await Brief(client=fake2).build(p2)
    assert texts(data2) == ["личный кабинет"]


async def test_reply_allows_rich_agreement(db):
    """Согласие с собственным содержанием — это уже слова клиента.

    Оно проходит по обычному пути (origin="client"), а не как подтверждённое
    предложение: своё сказанное сильнее нашей трактовки чужого «да».
    """
    p = await make_project()
    await add_msg(p.id, "1", "предлагаю вынести фильтры в боковую панель", roles.OWNER)
    async with Session() as s:
        s.add(ChatMessage(transport="telegram", chat_id=CHAT, tg_message_id="2",
                          project_id=p.id, sender_role=roles.CLIENT, sender_id="1",
                          text="да, давайте, только кнопку сделайте крупнее", reply_to="1"))
        await s.commit()

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "фильтры в боковой панели", "evidence": ev("1", "2")}]))
    data = await Brief(client=fake).build(p)
    assert texts(data) == ["фильтры в боковой панели"]
    assert data["deliverables"][0]["origin"] == ORIGIN_CLIENT

    # но связь предложение→согласие всё равно построена: голое «да» в reply
    # сработало бы и без собственного содержания
    async with Session() as s:
        messages = (await s.execute(
            select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
    assert find_confirmations(messages), "reply-связь не построилась"
