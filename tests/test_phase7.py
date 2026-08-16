"""Фаза 7: то, что нашли первые настоящие данные.

Каждый тест здесь закрывает дефект, вылезший на живой переписке из 132
сообщений, а не придуманный за столом.
"""
from __future__ import annotations

import pytest
from conftest import FakeCommunicator, make_project

from autopilot import brief as B
from autopilot import planner as P
from autopilot.config import cfg
from autopilot.db import ChatMessage, Project, Session


async def add_msg(project_id: int, text: str, role: str = "client",
                  mid: str = "1") -> ChatMessage:
    async with Session() as s:
        m = ChatMessage(transport="telegram", chat_id="c1", tg_message_id=mid,
                        project_id=project_id, direction="in", sender_id="42",
                        sender_role=role, text=text)
        s.add(m)
        await s.commit()
        return await s.get(ChatMessage, m.id)


# ---------- 1. обрезка контекста ----------

async def test_long_message_survives_context_budget(db):
    """Длинное сообщение доходит до модели целиком, а не обрубком в 120 знаков.

    Ровно этот дефект выбросил заполненный клиентом бриф на 3851 символ:
    он был 53-м с конца, попал в «сжимаемую» часть и дошёл как 120 символов.
    """
    p = await make_project()
    spec = "ТЗ: " + "нужен каталог, корзина, оплата картой, курьерское приложение. " * 60
    assert len(spec) > B.LONG_MESSAGE_CHARS * 5

    msgs = [await add_msg(p.id, "привет", mid="1"),
            await add_msg(p.id, spec, mid="2")]
    msgs += [await add_msg(p.id, f"ок {i}", mid=str(10 + i)) for i in range(120)]

    rendered, _ = B.Brief()._render(msgs)
    assert spec in rendered, "длинное сообщение обрублено"
    assert "курьерское приложение" in rendered


async def test_short_messages_dropped_before_long(db, monkeypatch):
    """Когда бюджет жмёт, жертвуем короткими, а длинные держим целиком.

    Длина — признак содержательности, а не мусорности. Резать по ней в обратную
    сторону значит систематически выбрасывать именно ТЗ.
    """
    monkeypatch.setattr(cfg, "brief_context_tokens", 900)
    p = await make_project()
    spec = "требования: " + "каталог и оплата. " * 100      # ~1800 символов
    msgs = [await add_msg(p.id, spec, mid="1")]
    msgs += [await add_msg(p.id, f"ок номер {i}", mid=str(10 + i)) for i in range(300)]

    rendered, _ = B.Brief()._render(msgs)
    assert spec in rendered, "выкинули длинное вместо коротких"
    assert "Опущено коротких реплик" in rendered
    assert rendered.count("ок номер") < 300, "ничего не опущено — тест бессмысленный"


async def test_whole_chat_fits_by_default(db):
    """Переписка на 15 тысяч символов уходит целиком: обрезать нечего."""
    p = await make_project()
    msgs = [await add_msg(p.id, "реплика " * 30, mid=str(i)) for i in range(60)]
    total = sum(len(m.text) for m in msgs)
    assert 10000 < total < 30000, "фикстура не похожа на живой чат"

    rendered, _ = B.Brief()._render(msgs)
    assert "Опущено" not in rendered
    for m in msgs:
        assert m.text in rendered


# ---------- 2. text_truncated — ошибка системы ----------

def test_truncation_complaints_detects_only_truncation():
    """Обрезанный текст и нечитаемое вложение — разные вещи."""
    cut = B.truncation_complaints({"unreadable": [
        {"message_id": "10", "kind": "photo"},
        {"message_id": "11", "kind": "text_truncated"},
        {"message_id": "12", "kind": "document"},
        {"message_id": "13", "kind": "message_truncated"},
    ]})
    assert cut == ["11", "13"], "перепутал обрезку текста с нечитаемым вложением"

    # вложения сами по себе провалом не являются: это свойство данных
    assert B.truncation_complaints({"unreadable": [
        {"message_id": "1", "kind": "photo"},
        {"message_id": "2", "kind": "voice"}]}) == []
    assert B.truncation_complaints({}) == []


async def test_truncated_text_fails_the_run(db, monkeypatch):
    """Модель сказала «текст обрезан» — бриф не принимается, владельцу сообщают."""
    p = await make_project()
    await add_msg(p.id, "нужен каталог товаров с фильтрами", mid="1")

    comm = FakeCommunicator()
    brief = B.Brief(communicator=comm)

    async def fake_attempt(prompt, project):
        return {
            "goal": {"text": "сайт", "evidence": ["telegram:c1:1"]},
            "deliverables": [], "stack": [], "constraints": [], "assets": [],
            "access_needed": [], "open_questions": [], "out_of_scope": [],
            "confidence": 0.9,
            "unreadable": [{"message_id": "1", "kind": "text_truncated"}],
        }
    monkeypatch.setattr(brief, "_attempt", fake_attempt)

    async with Session() as s:
        project = await s.get(Project, p.id)
    result = await brief.build(project)

    assert result is None, "бриф принят, хотя текст до модели дошёл обрезанным"
    assert comm.owner_notes, "владельцу не сказали про поломку нарезки"
    assert "обрезан" in comm.owner_notes[0].lower()


# ---------- 3. терпимый парсер брифа ----------

def test_brief_uses_tolerant_parser():
    """Тот же разбор, что у судьи: проза перед JSON не должна валить прогон."""
    from autopilot.verifier import parse_judge_json

    raw = ('Вот собранный бриф по переписке:\n\n'
           '{"goal": {"text": "сайт", "evidence": ["1"]}, "confidence": 0.8}')
    data, extracted = parse_judge_json(raw)
    assert extracted is True and data["goal"]["text"] == "сайт"


def test_brief_max_tokens_raised():
    """Потолок ответа поднят: на 4000 JSON брифа обрывался на середине."""
    assert cfg.brief_max_tokens >= 8000


# ---------- 4. задача может происходить из ЦЕЛИ ----------

def test_task_from_goal_is_kept_and_marked():
    """Ссылка на цель законна и помечается origin=goal.

    Из-за её запрета из живого плана пропала публикация в App Store
    и Google Play — то, чем проект заканчивается.
    """
    brief = {
        "goal": {"text": "Приложение доставки с публикацией в App Store и Google Play",
                 "evidence": ["1"]},
        "deliverables": [{"text": "Клиентская часть приложения", "evidence": ["1"],
                          "priority": "must"}],
    }
    tasks = [
        {"title": "Подготовка к публикации в App Store",
         "deliverable_ref": "Приложение доставки с публикацией в App Store и Google Play",
         "acceptance": [{"type": "file_exists", "path": "app.json"}],
         "verify_class": "auto", "executor": "claude_code", "depends_on": [],
         "estimate_min": 60},
        {"title": "Экран каталога",
         "deliverable_ref": "Клиентская часть приложения",
         "acceptance": [{"type": "file_exists", "path": "src/Catalog.tsx"}],
         "verify_class": "auto", "executor": "claude_code", "depends_on": [],
         "estimate_min": 90},
    ]
    out, notes = P.Planner(client=object()).normalize(tasks, brief, [])

    titles = {t["title"] for t in out}
    assert "Подготовка к публикации в App Store" in titles, "задача из цели выброшена"
    origins = {t["title"]: t["ref_origin"] for t in out}
    assert origins["Подготовка к публикации в App Store"] == "goal"
    assert origins["Экран каталога"] == "deliverable"
    assert not any("не найден в ТЗ" in n for n in notes)


def test_task_from_nowhere_still_dropped():
    """Правило против выдуманной работы осталось: ссылка обязана на что-то указывать."""
    brief = {"goal": {"text": "интернет-магазин", "evidence": ["1"]},
             "deliverables": [{"text": "Каталог", "evidence": ["1"], "priority": "must"}]}
    tasks = [{"title": "Написать мобильное приложение",
              "deliverable_ref": "Мобильное приложение под iOS",
              "acceptance": [{"type": "shell", "cmd": "true"}],
              "verify_class": "auto", "executor": "claude_code",
              "depends_on": [], "estimate_min": 10}]
    out, notes = P.Planner(client=object()).normalize(tasks, brief, [])
    assert out == []
    assert any("не найден в ТЗ" in n for n in notes)


# ---------- 5. стек — вопрос, а не решение ----------

def test_stack_declared_predicate():
    assert P.stack_declared({"stack": [{"text": "WordPress", "evidence": ["1"]}]}) is True
    # пустой список — это «не обсуждали», а не «любой»
    assert P.stack_declared({"stack": []}) is False
    assert P.stack_declared({}) is False
    assert P.stack_declared({"stack": [{"text": "  "}]}) is False


async def test_planner_asks_about_stack_instead_of_choosing(db, monkeypatch):
    """Стека в ТЗ нет — планировщик ставит блокирующий вопрос и НЕ планирует.

    На живом проекте молчаливый выбор React Native попал в первую задачу,
    от которой зависели все остальные. Клиент об этом не знал.
    """
    p = await make_project()
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.brief = {"brief": {
            "goal": {"text": "приложение доставки", "evidence": ["1"]},
            "deliverables": [{"text": "Клиентская часть", "evidence": ["1"],
                              "priority": "must"}],
            "stack": [],
        }}
        await s.commit()
        project = await s.get(Project, p.id)

    comm = FakeCommunicator()
    planner = P.Planner(client=object(), communicator=comm)
    planned = []
    monkeypatch.setattr(planner, "_attempt",
                        lambda *a, **k: planned.append(1) or [])
    monkeypatch.setattr(planner, "_suggest_stack",
                        _async("Предлагаю React Native. Это предложение, "
                               "требующее согласования с клиентом."))

    result = await planner.plan(project)

    assert result is None, "планировщик выбрал стек за клиента"
    assert not planned, "модель планирования всё-таки вызвана"

    async with Session() as s:
        row = await s.get(Project, p.id)
    questions = (row.brief or {}).get("brief", {}).get("open_questions") or []
    assert questions, "вопрос о стеке не поставлен"
    assert questions[0]["blocking"] is True
    assert "технолог" in questions[0]["text"].lower()
    assert row.brief_ready is False, "блокирующий вопрос не держит проект"
    assert comm.owner_notes, "владельцу не сообщили"


async def test_planner_plans_when_stack_is_declared(db, monkeypatch):
    """Стек в ТЗ есть — планируем по нему и вопрос не задаём."""
    p = await make_project()
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.brief = {"brief": {
            "goal": {"text": "сайт", "evidence": ["1"]},
            "deliverables": [{"text": "Каталог", "evidence": ["1"], "priority": "must"}],
            "stack": [{"text": "WordPress + WooCommerce", "evidence": ["1"]}],
        }}
        await s.commit()
        project = await s.get(Project, p.id)

    planner = P.Planner(client=object(), communicator=FakeCommunicator())
    called = []

    async def fake_attempt(prompt, project):
        called.append(prompt)
        return [{"title": "Установить WooCommerce", "deliverable_ref": "Каталог",
                 "acceptance": [{"type": "file_exists", "path": "wp-config.php"}],
                 "verify_class": "auto", "executor": "claude_code",
                 "depends_on": [], "estimate_min": 30}]
    monkeypatch.setattr(planner, "_attempt", fake_attempt)

    result = await planner.plan(project)
    assert result is not None and called, "не стал планировать при заданном стеке"
    assert len(result["tasks"]) == 1


def _async(value):
    async def inner(*a, **k):
        return value
    return inner


# ---------- 6. пробный импорт ничего не меняет ----------

def test_dry_scrub_does_not_store(tmp_path, monkeypatch):
    """`--dry-run` не должен класть секреты в хранилище.

    Раньше клал: один и тот же пароль оседал в vault дважды — под именем
    от пробного прогона и под именем от боевого.
    """
    from autopilot.secrets_scan import scrub
    from cryptography.fernet import Fernet

    from autopilot.vault import Vault

    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    text = "пароль: SuperSecret123"

    out, names = scrub(text, project_id=1, chat_id="c", vault=v, store=False)
    assert "SuperSecret123" not in out, "секрет остался в тексте"
    assert v.names() == [], "пробный прогон записал секрет в хранилище"

    out2, names2 = scrub(text, project_id=1, chat_id="c", vault=v, store=True)
    assert "SuperSecret123" not in out2
    assert len(v.names()) == 1, "боевой прогон секрет не сохранил"
