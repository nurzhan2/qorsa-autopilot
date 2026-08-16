"""Фаза 7: то, что нашли первые настоящие данные.

Каждый тест здесь закрывает дефект, вылезший на живой переписке из 132
сообщений, а не придуманный за столом.
"""
from __future__ import annotations

from pathlib import Path

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


# ---------- 5. стек выбирает планировщик ----------

def test_stack_declared_predicate():
    assert P.stack_declared({"stack": [{"text": "WordPress", "evidence": ["1"]}]}) is True
    # пустой список — это «не обсуждали», а не «любой»
    assert P.stack_declared({"stack": []}) is False
    assert P.stack_declared({}) is False
    assert P.stack_declared({"stack": [{"text": "  "}]}) is False


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


# ---------- фаза 8: срок и бюджет как рамки решения ----------

HEAVY = ("микросервис", "kubernetes", "k8s", "api gateway", "kong",
         "rabbitmq", "kafka", "очеред", "service mesh", "redis")


def _brief_with_frame(deadline="через 2 недели, к началу сентября",
                      budget="150 000 рублей"):
    return {
        "goal": {"text": "приложение доставки в отели одного города", "evidence": ["1"]},
        "deliverables": [{"text": "Клиентское приложение", "evidence": ["1"],
                          "priority": "must"}],
        "constraints": [{"text": "около 50 заказов в день на старте", "evidence": ["1"]}],
        "stack": [],
        "deadline": {"text": deadline, "date": None, "evidence": ["1"]},
        "budget": {"text": budget, "amount": 150000, "currency": "RUB",
                   "evidence": ["1"]},
    }


def test_constraints_block_carries_frame_to_prompt():
    """Срок, бюджет и нагрузка попадают в промпт планировщика.

    Без этих трёх строчек планировщик выбирал микросервисы за API Gateway
    на приложение для одного города с полусотней заказов в день.
    """
    block = P.constraints_block(_brief_with_frame())
    assert "Срок" in block and "2 недели" in block
    assert "Бюджет" in block and "150" in block
    assert "Нагрузка" in block and "50 заказов" in block
    assert "поместиться" in block, "не объяснили, что это границы решения"

    # рамок нет — блока нет, пустой заголовок в промпт не тащим
    assert P.constraints_block({"goal": {"text": "сайт"}}) == ""


def test_prompt_tells_planner_to_prefer_simple():
    """Правило по умолчанию прописано в промпте явно."""
    assert "САМОЕ ПРОСТОЕ РЕШЕНИЕ" in P.SYSTEM
    for word in ("Микросервисы", "Kubernetes"):
        assert word in P.SYSTEM
    assert "stack_decision" in P.SYSTEM, "модель не просят обосновать выбор"


async def test_tight_frame_forbids_heavy_architecture(db, monkeypatch):
    """Две недели и 150К — план не должен содержать тяжёлой инфраструктуры.

    Проверяем контракт кода: рамки доехали до промпта, а решение записано
    в артефакт. Сам выбор делает модель, и здесь она подменена — иначе тест
    проверял бы не наш код, а её вкус.
    """
    p = await make_project()
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.brief = {"brief": _brief_with_frame()}
        await s.commit()
        project = await s.get(Project, p.id)

    planner = P.Planner(client=object(), communicator=FakeCommunicator())
    seen = {}

    async def fake_attempt(prompt, proj):
        seen["prompt"] = prompt
        return ([{"title": "Собрать монолит", "deliverable_ref": "Клиентское приложение",
                  "acceptance": [{"type": "file_exists", "path": "package.json"}],
                  "verify_class": "auto", "executor": "claude_code",
                  "depends_on": [], "estimate_min": 120}],
                {"chosen": ["Монолит на Django", "PostgreSQL"],
                 "rationale": "50 заказов в день и две недели — делить нечего",
                 "driven_by": ["срок 2 недели", "бюджет 150 000"],
                 "rejected": [{"option": "Микросервисы",
                               "why": "нагрузка не требует, срок не позволяет"}]})
    monkeypatch.setattr(planner, "_attempt", fake_attempt)

    result = await planner.plan(project)
    assert result is not None

    # 1) рамки реально доехали до модели
    prompt = seen["prompt"].lower()
    assert "рамки проекта" in prompt
    assert "2 недели" in prompt and "150" in prompt and "50 заказов" in prompt

    # 2) выбранное решение — не тяжёлое
    chosen = " ".join(result["stack_decision"]["chosen"]).lower()
    assert not any(w in chosen for w in ("микросервис", "kubernetes", "kong")), chosen

    # 3) решение записано артефактом, который переживёт сессию
    path = Path(result["decisions_path"])
    assert path.exists() and path.name == "DECISIONS.md"
    text = path.read_text(encoding="utf-8")
    assert "Монолит на Django" in text
    assert "Микросервисы" in text and "не требует" in text, "не записали отброшенное"
    assert "срок 2 недели" in text


async def test_planner_plans_when_stack_is_declared(db, monkeypatch):
    """Стек в ТЗ есть — планируем по нему и не подменяем."""
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

    async def fake_attempt(prompt, proj):
        assert "WordPress" in prompt, "стек из ТЗ не доехал до планировщика"
        return ([{"title": "Установить WooCommerce", "deliverable_ref": "Каталог",
                  "acceptance": [{"type": "file_exists", "path": "wp-config.php"}],
                  "verify_class": "auto", "executor": "claude_code",
                  "depends_on": [], "estimate_min": 30}],
                {"chosen": ["WordPress + WooCommerce"], "rationale": "взят из ТЗ"})
    monkeypatch.setattr(planner, "_attempt", fake_attempt)

    result = await planner.plan(project)
    assert result is not None and len(result["tasks"]) == 1
    assert result["stack_decision"].get("from_brief") is True


# ---------- бриф извлекает деньги и сроки, но не торгуется ----------

def test_money_filter_separates_our_fee_from_product_features():
    """Деньги внутри продукта — функциональность, а не коммерция.

    Прежний фильтр убивал и то и другое одним корнем слова. На живом ТЗ он
    выбросил бы промокоды, минимальную сумму заказа и расчёт стоимости
    доставки — то есть три настоящих требования.
    """
    # наши деньги и сроки — не зона брифа
    for text in ("предоплата 50 процентов",
                 "порядок оплаты работ",
                 "бюджет проекта обсудим позже",
                 "сколько стоит разработка"):
        assert B.is_commercial(text) is True, text

    # деньги внутри продукта клиента — законные требования
    for text in ("промокоды и скидки для покупателей",
                 "минимальная сумма заказа 3000 ₽",
                 "расчёт стоимости доставки по расстоянию",
                 "оплата картой и СБП в корзине",
                 "чтобы люди сами оформляли заказ и оплачивали онлайн"):
        assert B.is_commercial(text) is False, text


def test_reference_fields_in_schema():
    """deadline и budget — часть схемы и часть пустого брифа."""
    assert B.REFERENCE_FIELDS == ("deadline", "budget")
    empty = B.empty_brief()
    assert empty["deadline"] is None and empty["budget"] is None
    assert "deadline" in B.SYSTEM and "budget" in B.SYSTEM
    assert "извлекать ОБЯЗАН, обсуждать — НЕТ" in B.SYSTEM


async def test_brief_extracts_deadline_and_budget_but_not_as_work(db, monkeypatch):
    """«Хочу к сентябрю, бюджет 150К» -> заполненные deadline и budget,
    и НИ ОДНОГО пункта deliverables про деньги."""
    p = await make_project()
    await add_msg(p.id, "Нужно приложение доставки. Первая версия к началу "
                        "сентября, бюджет 150 тысяч рублей.", mid="1")

    brief = B.Brief(communicator=FakeCommunicator())

    async def fake_attempt(prompt, project):
        return {
            "goal": {"text": "приложение доставки", "evidence": ["telegram:c1:1"]},
            "deliverables": [{"text": "Приложение доставки", "priority": "must",
                              "priority_reason": "«нужно приложение»",
                              "evidence": ["telegram:c1:1"]}],
            "stack": [], "constraints": [], "assets": [], "access_needed": [],
            "open_questions": [], "out_of_scope": [],
            "deadline": {"text": "первая версия к началу сентября",
                         "date": "2026-09-01", "evidence": ["telegram:c1:1"]},
            "budget": {"text": "150 тысяч рублей", "amount": 150000,
                       "currency": "RUB", "evidence": ["telegram:c1:1"]},
            "confidence": 0.9, "unreadable": [],
        }
    monkeypatch.setattr(brief, "_attempt", fake_attempt)

    async with Session() as s:
        project = await s.get(Project, p.id)
    data = await brief.build(project)

    assert data is not None
    assert data["deadline"]["text"] == "первая версия к началу сентября"
    assert data["budget"]["amount"] == 150000

    # и ни одного пункта работ про деньги
    texts = " ".join(B.item_text(d) for d in data["deliverables"]).lower()
    for word in ("бюджет", "предоплат", "150", "оплата работ"):
        assert word not in texts, f"деньги уехали в объём работ: {word}"

    # срок доехал до проекта — оттуда его берут таблица и WFQ
    async with Session() as s:
        row = await s.get(Project, p.id)
    assert row.deadline is not None and row.deadline.isoformat() == "2026-09-01"


def test_deadline_parsed_only_from_machine_date():
    """Словесный срок в дату не превращаем: ошибка молча сдвинет приоритет."""
    assert B.parse_brief_deadline(
        {"deadline": {"text": "к сентябрю", "date": "2026-09-01"}}).isoformat() == "2026-09-01"
    assert B.parse_brief_deadline({"deadline": {"text": "к концу августа"}}) is None
    assert B.parse_brief_deadline({"deadline": {"text": "х", "date": "скоро"}}) is None
    assert B.parse_brief_deadline({}) is None


# ---------- вопросы клиента к нам ----------

def test_client_questions_survive_money_filter():
    """Вопрос клиента про стоимость доставки не должен глушиться фильтром.

    Вопрос ничего не обещает клиенту, а потерять его — значит не узнать,
    что объём работ до сих пор не определён.
    """
    brief = B.Brief()
    data = {
        "goal": {"text": "приложение", "evidence": ["telegram:c1:1"]},
        "deliverables": [],
        "open_questions": [
            {"text": "Нужен ли расчёт стоимости по доставке?", "blocking": True,
             "evidence": ["telegram:c1:1"]},
            {"text": "Про платёжные системы я не совсем понимаю, что нужно",
             "blocking": True, "evidence": ["telegram:c1:1"]},
        ],
        "stack": [], "constraints": [], "assets": [], "access_needed": [],
        "out_of_scope": [], "confidence": 0.8, "unreadable": [],
    }
    msgs = [_msg("1", "client", "Нужен ли расчёт стоимости по доставке! "
                                "Про платежные системы я не совсем понимаю что нужно")]
    checked, dropped = brief.apply_evidence(data, msgs)

    kept = [q["text"] for q in checked["open_questions"]]
    assert len(kept) == 2, f"вопросы клиента потерялись: {dropped}"
    assert any("стоимост" in t.lower() for t in kept)


def test_prompt_asks_for_client_questions():
    assert "ВОПРОСЫ, КОТОРЫЕ КЛИЕНТ ЗАДАЛ НАМ" in B.SYSTEM


def _msg(mid, role, text):
    from autopilot.db import ChatMessage
    m = ChatMessage(transport="telegram", chat_id="c1", tg_message_id=mid,
                    project_id=1, direction="in", sender_id="42",
                    sender_role=role, text=text)
    m.id = int(mid)
    return m


def test_deadline_in_deep_past_is_rejected():
    """Прошлогодняя дата — ошибка разбора, а не просроченный проект.

    Поймано на живом чате: модель не знала сегодняшнего числа и превратила
    «начало сентября» в прошлогоднее. WFQ даёт просроченному проекту
    четырёхкратный вес — один неверный год поднял бы его над всеми навсегда.
    """
    import datetime as dt

    old = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    assert B.parse_brief_deadline({"deadline": {"text": "к сентябрю", "date": old}}) is None

    soon = (dt.date.today() + dt.timedelta(days=20)).isoformat()
    assert B.parse_brief_deadline({"deadline": {"text": "скоро", "date": soon}}) is not None

    # недавно просроченный срок — законная ситуация, его не глушим
    recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    assert B.parse_brief_deadline({"deadline": {"text": "вчера", "date": recent}}) is not None


def test_brief_prompt_carries_today():
    """Без сегодняшнего числа относительный срок превращается в дату наугад."""
    import inspect

    src = inspect.getsource(B.Brief.build_prompt)
    assert "# Сегодня" in src


def test_constraints_block_counts_days_left_itself():
    """Остаток до срока считает КОД, а не модель.

    Модель не знает сегодняшнего числа: «сентябрь 2026» она прочитала как
    «через 14 месяцев», хотя до него было две недели. От этой цифры зависит
    выбор решения — гадать её нельзя.
    """
    import datetime as dt

    soon = (dt.date.today() + dt.timedelta(days=14)).isoformat()
    block = P.constraints_block({
        "deadline": {"text": "к началу сентября", "date": soon, "evidence": ["1"]}})
    assert "Сегодня:" in block, "планировщик не знает текущей даты"
    assert "это 14 дней от сегодня" in block

    past = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    overdue = P.constraints_block({
        "deadline": {"text": "вчера", "date": past, "evidence": ["1"]}})
    assert "СРОК УЖЕ ПРОШЁЛ 3 дней назад" in overdue
