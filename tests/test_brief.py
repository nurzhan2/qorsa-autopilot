"""brief.py: коду принадлежит последнее слово, модель только предлагает."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import make_project
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from autopilot import roles
from autopilot.brief import (Brief, BriefRunner, SecretLeak, assert_no_secrets,
                             merge, pending_questions)
from autopilot.communicator import Communicator
from autopilot.config import cfg
from autopilot.db import AccessItem, ChatMessage, Message, Project, Session, utcnow
from autopilot.vault import Vault

CHAT = "-100500"


class FakeAnthropic:
    """Подменяет клиента Anthropic: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.messages = self

    async def create(self, *, model, max_tokens, system=None, messages, **kw):
        self.prompts.append(messages[0]["content"])
        self.systems.append(system or "")
        body = self.replies.pop(0) if self.replies else "{}"
        return _Resp(body)


class _Resp:
    def __init__(self, text: str):
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Usage:
    input_tokens = 1000
    output_tokens = 200


async def add_msg(project_id: int | None, mid: str, text: str, role: str = roles.CLIENT,
                  *, has_media: bool = False, media_kind: str | None = None,
                  chat_id: str = CHAT, transport: str = "telegram") -> ChatMessage:
    async with Session() as s:
        m = ChatMessage(transport=transport, chat_id=chat_id, tg_message_id=mid,
                        project_id=project_id, sender_role=role,
                        sender_id={"client": "1", "manager": "2", "owner": "3"}.get(role, "9"),
                        text=text, has_media=has_media, media_kind=media_kind,
                        created_at=utcnow())
        s.add(m)
        await s.commit()
    return m


def ev(*ids: str) -> list[str]:
    return [f"telegram:{CHAT}:{i}" for i in ids]


def reply(goal="сделать интернет-магазин", goal_ev=("1",), **fields) -> str:
    data = {
        "goal": {"text": goal, "evidence": ev(*goal_ev)},
        "deliverables": [], "stack": [], "constraints": [], "assets": [],
        "access_needed": [], "open_questions": [], "out_of_scope": [],
        "confidence": 0.9, "unreadable": [],
    }
    data.update(fields)
    # с фазы 3.3 модальность у deliverables обязательна по схеме;
    # тестам, которым она не важна, проставляем must
    for item in data["deliverables"]:
        if isinstance(item, dict):
            item.setdefault("priority", "must")
    # с фазы 4 у вопроса обязательно blocking; по умолчанию считаем
    # блокирующим — так тесты фаз 3.x сохраняют прежний смысл
    for item in data["open_questions"]:
        if isinstance(item, dict):
            item.setdefault("blocking", True)
    return json.dumps(data, ensure_ascii=False)


# ---------- роли ----------

async def test_roles_assigned(db, monkeypatch):
    """Механизм ролей цел: при MANAGER_SEPARATE=1 роли раздаются все четыре.

    По умолчанию менеджер — алиас владельца, это проверяет
    test_manager_role_absent. Здесь важно, что механизм не выродился.
    """
    monkeypatch.setattr(cfg, "manager_separate", True)
    monkeypatch.setattr(cfg, "owner_tg_id", "777")
    monkeypatch.setattr(cfg, "manager_tg_id", "888")
    monkeypatch.setattr(cfg, "bot_tg_id", "999")

    assert roles.role_of("telegram", "888") == roles.MANAGER
    assert roles.role_of("telegram", "777") == roles.OWNER
    assert roles.role_of("telegram", "999") == roles.BOT
    assert roles.role_of("telegram", "123") == roles.CLIENT
    assert roles.role_of("telegram", None) == roles.CLIENT

    for sender, expected in (("888", roles.MANAGER), ("777", roles.OWNER),
                             ("999", roles.BOT), ("123", roles.CLIENT)):
        assert await roles.remember("telegram", CHAT, sender, "имя") == expected

    async with Session() as s:
        rows = {p.sender_id: p.role for p in
                (await s.execute(select(__import__("autopilot.db", fromlist=["x"])
                                        .ChatParticipant))).scalars()}
    assert rows == {"888": "manager", "777": "owner", "999": "bot", "123": "client"}


# ---------- evidence ----------

async def test_brief_ignores_manager(db):
    """Реплика менеджера не попадает в бриф, даже если это требование."""
    p = await make_project(title="магазин")
    await add_msg(p.id, "1", "хочу интернет-магазин на битриксе", roles.CLIENT)
    await add_msg(p.id, "2", "и обязательно интеграцию с 1С сделаем", roles.MANAGER)

    fake = FakeAnthropic(reply(
        deliverables=[
            {"text": "интернет-магазин", "evidence": ev("1")},
            {"text": "интеграция с 1С", "evidence": ev("2")},   # слова менеджера
        ]))
    data = await Brief(client=fake).build(p)

    texts = [d["text"] for d in data["deliverables"]]
    assert "интернет-магазин" in texts
    assert "интеграция с 1С" not in texts, "требование менеджера уехало в ТЗ клиента"


async def test_evidence_required(db):
    """Пункт без evidence отбрасывается."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен сайт", roles.CLIENT)

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "сайт", "evidence": ev("1")},
        {"text": "мобильное приложение", "evidence": []},
        {"text": "и ещё телеграм-бот"},
    ]))
    data = await Brief(client=fake).build(p)

    assert [d["text"] for d in data["deliverables"]] == ["сайт"]
    meta = (await _reload(p)).brief["_meta"]
    assert any("deliverables[1]" in d for d in meta["dropped"])


async def test_evidence_hallucinated(db):
    """Несуществующий id отбрасывает пункт."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен сайт", roles.CLIENT)

    fake = FakeAnthropic(reply(deliverables=[
        {"text": "сайт", "evidence": ev("1")},
        {"text": "интеграция с CRM", "evidence": ev("4242")},
    ]))
    data = await Brief(client=fake).build(p)

    assert [d["text"] for d in data["deliverables"]] == ["сайт"]
    meta = (await _reload(p)).brief["_meta"]
    assert any("выдуманные" in d for d in meta["dropped"])


async def test_no_price_in_brief(db):
    """Обсуждение цены и сроков в бриф не попадает."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен лендинг", roles.CLIENT)
    await add_msg(p.id, "2", "бюджет 300 000, надо к 1 сентября", roles.CLIENT)

    fake = FakeAnthropic(reply(
        goal="лендинг", goal_ev=("1",),
        deliverables=[
            {"text": "лендинг", "evidence": ev("1")},
            {"text": "уложиться в бюджет 300 000", "evidence": ev("2")},
            {"text": "сдать к 1 сентября, срок жёсткий", "evidence": ev("2")},
        ]))
    data = await Brief(client=fake).build(p)

    texts = " ".join(d["text"] for d in data["deliverables"])
    assert "лендинг" in texts
    assert "300 000" not in texts and "бюджет" not in texts
    assert "срок" not in texts


# ---------- секреты ----------

async def test_secret_never_in_llm_prompt(db, tmp_path, monkeypatch):
    """Значение из vault не уходит в API: запрос вообще не отправляется."""
    secret = "Str0ngP@ss-brief-canary"
    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    v.set("FTP", secret)

    p = await make_project()
    # сообщение с секретом в открытом виде (например, попало мимо scrub)
    await add_msg(p.id, "1", f"пароль {secret}", roles.CLIENT)

    fake = FakeAnthropic(reply())
    comm = Recorder()
    result = await Brief(client=fake, vault=v, communicator=comm).build(p)

    assert result is None
    assert fake.prompts == [], "запрос с секретом всё-таки ушёл в API"
    assert comm.owner_notes, "владельцу не сообщили об остановке"
    assert (await _reload(p)).status == "blocked"

    with pytest.raises(SecretLeak):
        assert_no_secrets(f"...{secret}...", v)
    assert_no_secrets("безобидный текст", v)


# ---------- схема ----------

async def test_schema_retry(db):
    """Невалидный JSON → повтор → успех; два провала → эскалация."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен сайт", roles.CLIENT)

    fake = FakeAnthropic("это не json вовсе", reply())
    data = await Brief(client=fake).build(p)
    assert data is not None and data["goal"]["text"]
    assert len(fake.prompts) == 2
    assert "исправь" in fake.prompts[1]

    p2 = await make_project(title="второй")
    await add_msg(p2.id, "10", "нужен сайт", roles.CLIENT)
    comm = Recorder()
    bad = FakeAnthropic("мусор", json.dumps({"goal": {"text": 5}, "confidence": 42}))
    assert await Brief(client=bad, communicator=comm).build(p2) is None
    assert (await _reload(p2)).status == "blocked"
    assert comm.owner_notes


# ---------- инкрементальность ----------

async def test_incremental_update(db):
    """Новые сообщения обновляют бриф, старые пункты не теряются."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен интернет-магазин", roles.CLIENT)

    first = FakeAnthropic(reply(deliverables=[{"text": "каталог", "evidence": ev("1")}]))
    data = await Brief(client=first).build(p)
    assert [d["text"] for d in data["deliverables"]] == ["каталог"]

    p = await _reload(p)
    await add_msg(p.id, "2", "ещё нужна корзина", roles.CLIENT)

    second = FakeAnthropic(reply(deliverables=[{"text": "корзина", "evidence": ev("2")}]))
    data = await Brief(client=second).build(p)

    texts = [d["text"] for d in data["deliverables"]]
    assert "корзина" in texts
    assert "каталог" in texts, "инкремент затёр ранее собранное"
    # второй раз подаём только новые реплики плюс предыдущий бриф
    assert "ещё нужна корзина" in second.prompts[0]
    assert "Предыдущая версия брифа" in second.prompts[0]

    # без новых сообщений модель не дёргаем вовсе
    idle = FakeAnthropic(reply())
    await Brief(client=idle).build(await _reload(p))
    assert idle.prompts == []


def test_merge_keeps_confirmed():
    prev = {"goal": {"text": "магазин", "evidence": ["a"]},
            "deliverables": [{"text": "каталог", "evidence": ["a"]}],
            "open_questions": [{"text": "какой домен?", "evidence": ["a"]}],
            "confidence": 0.5}
    fresh = {"goal": None, "deliverables": [{"text": "корзина", "evidence": ["b"]}],
             "open_questions": [], "confidence": 0.9}
    out = merge(prev, fresh)
    assert out["goal"]["text"] == "магазин"
    assert [d["text"] for d in out["deliverables"]] == ["каталог", "корзина"]
    assert out["open_questions"] == [], "закрытые вопросы не должны возвращаться"
    assert out["confidence"] == 0.9


# ---------- доступы ----------

async def test_access_items_created(db, monkeypatch):
    """Дубли не плодятся, stale не стирает verified."""
    p = await make_project()
    await add_msg(p.id, "1", "хостинг на бегете, репозиторий на гитхабе", roles.CLIENT)

    access = [{"kind": "hosting_panel", "name": "Панель Beget", "evidence": ev("1")},
              {"kind": "git", "name": "Репозиторий", "evidence": ev("1")}]
    await Brief(client=FakeAnthropic(reply(access_needed=access))).build(await _reload(p))

    async with Session() as s:
        items = (await s.execute(select(AccessItem).order_by(AccessItem.id))).scalars().all()
    assert {i.name for i in items} == {"Панель Beget", "Репозиторий"}
    assert all(i.status == "needed" and i.source == "brief" for i in items)

    # доступ пришёл и проверен
    async with Session() as s:
        row = await s.get(AccessItem, items[0].id)
        row.status = "verified"
        await s.commit()

    # повторный сбор с тем же списком не создаёт дублей
    await add_msg(p.id, "2", "и ещё домен у reg.ru", roles.CLIENT)
    access2 = access + [{"kind": "domain", "name": "Домен", "evidence": ev("2")}]
    await Brief(client=FakeAnthropic(reply(access_needed=access2))).build(await _reload(p))

    async with Session() as s:
        n = (await s.execute(select(func.count()).select_from(AccessItem))).scalar_one()
    assert n == 3, "пункты доступа задвоились"

    # Модель не вернула часть пунктов в очередном прогоне. С фазы 3.3 это НЕ
    # повод их терять: бриф накапливает, пункт остаётся с пометкой missing,
    # а пункт чеклиста продолжает считаться нужным
    monkeypatch.setattr(cfg, "brief_full_rebuild_every", 1)
    await add_msg(p.id, "3", "и ещё вот", roles.CLIENT)
    data = await Brief(client=FakeAnthropic(reply(access_needed=[access[1]]))).build(
        await _reload(p))

    names = {i["name"]: i for i in data["access_needed"]}
    assert names["Домен"].get("missing") is True, "пропавший пункт должен быть помечен"
    assert names["Репозиторий"].get("missing") is None

    async with Session() as s:
        rows = {i.name: i for i in (await s.execute(select(AccessItem))).scalars()}
    assert rows["Панель Beget"].status == "verified"
    assert rows["Панель Beget"].stale is False, "проверенный доступ нельзя ронять в stale"
    assert rows["Домен"].stale is False, "пункт не выброшен, а лишь не подтверждён прогоном"
    assert rows["Репозиторий"].stale is False


# ---------- уверенность и вопросы ----------

async def test_low_confidence_blocks(db):
    """Низкая уверенность не пускает проект дальше."""
    p = await make_project()
    await add_msg(p.id, "1", "хочу что-нибудь красивое", roles.CLIENT)

    await Brief(client=FakeAnthropic(reply(confidence=0.4))).build(p)
    fresh = await _reload(p)
    assert fresh.brief_ready is False

    await add_msg(p.id, "2", "точнее: сайт-визитка на две страницы", roles.CLIENT)
    await Brief(client=FakeAnthropic(reply(confidence=0.95))).build(fresh)
    assert (await _reload(p)).brief_ready is True


async def test_open_questions_block(db):
    p = await make_project()
    await add_msg(p.id, "1", "нужен сайт", roles.CLIENT)
    await Brief(client=FakeAnthropic(reply(
        confidence=0.99,
        open_questions=[{"text": "какой домен?", "evidence": ev("1")}]))).build(p)

    fresh = await _reload(p)
    assert fresh.brief_ready is False
    assert await pending_questions(fresh) == ["какой домен?"]


async def test_question_cooldown(db, monkeypatch):
    """За период уходит один пакет вопросов, не больше трёх штук."""
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    p = await make_project()
    comm = Communicator()

    questions = ["вопрос один", "вопрос два", "вопрос три", "вопрос четыре"]
    first = await comm.ask_questions(p, questions)
    assert first is not None
    assert "вопрос три" in first.text
    assert "вопрос четыре" not in first.text, "больше трёх вопросов за раз не задаём"

    for _ in range(3):
        assert await comm.ask_questions(p, questions) is None

    async with Session() as s:
        n = (await s.execute(select(func.count()).select_from(Message)
                             .where(Message.kind == "brief_questions"))).scalar_one()
    assert n == 1

    # кулдаун истёк — можно снова
    async with Session() as s:
        m = await s.get(Message, first.id)
        m.created_at = utcnow() - dt.timedelta(hours=cfg.brief_question_cooldown_h + 1)
        await s.commit()
    assert await comm.ask_questions(p, questions) is not None


# ---------- вложения ----------

async def test_unreadable_media(db):
    """Вложение попадает в unreadable[] даже если модель про него забыла."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен сайт", roles.CLIENT)
    await add_msg(p.id, "2", "", roles.CLIENT, has_media=True, media_kind="voice")
    await add_msg(p.id, "3", "вот макет", roles.CLIENT, has_media=True, media_kind="photo")

    fake = FakeAnthropic(reply())
    data = await Brief(client=fake).build(p)

    kinds = {u["message_id"]: u["kind"] for u in data["unreadable"]}
    assert kinds == {"2": "voice", "3": "photo"}
    # и модель предупреждена, что содержимого не видит
    assert "ВЛОЖЕНИЕ" in fake.prompts[0]


async def test_runner_asks_and_skips_ready(db, monkeypatch):
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    p = await make_project()
    await add_msg(p.id, "1", "нужен сайт", roles.CLIENT)

    comm = Communicator()
    runner = BriefRunner(Brief(client=FakeAnthropic(reply(
        confidence=0.5,
        open_questions=[{"text": "какой домен?", "evidence": ev("1")}]))), comm)
    assert await runner.tick() == 1

    async with Session() as s:
        msgs = (await s.execute(select(Message)
                                .where(Message.kind == "brief_questions"))).scalars().all()
    assert len(msgs) == 1
    assert "какой домен?" in msgs[0].text


class Recorder:
    def __init__(self):
        self.owner_notes: list[str] = []

    async def notify_owner(self, text):
        self.owner_notes.append(text)

    async def ask_questions(self, project, questions):
        return None


async def _reload(p: Project) -> Project:
    async with Session() as s:
        return await s.get(Project, p.id)


async def test_payment_feature_is_not_commerce(db):
    """Оплата картой — это функция магазина, а не разговор о деньгах.

    Регрессия: первая версия постфильтра глушила любое «оплат» и на живом
    чате выбросила центральное требование клиента.
    """
    p = await make_project()
    await add_msg(p.id, "1", "нужен магазин, чтобы заказ оформляли и оплачивали онлайн",
                  roles.CLIENT)
    await add_msg(p.id, "2", "ориентировочно 450 000 тенге", roles.CLIENT)

    fake = FakeAnthropic(reply(
        goal="магазин с онлайн-оплатой", goal_ev=("1",),
        deliverables=[
            {"text": "оформление заказа и оплата картой онлайн", "evidence": ev("1")},
            {"text": "страница про доставку и оплату", "evidence": ev("1")},
            {"text": "уложиться в 450 000 тенге", "evidence": ev("2")},
        ]))
    data = await Brief(client=fake).build(p)

    texts = [d["text"] for d in data["deliverables"]]
    assert "оформление заказа и оплата картой онлайн" in texts
    assert "страница про доставку и оплату" in texts
    assert not any("450 000" in t for t in texts)
    assert data["goal"] is not None, "цель выброшена из-за слова «оплата»"
