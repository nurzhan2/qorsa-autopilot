"""planner.py: класс проверяемости решает код, а не модель."""
from __future__ import annotations

import json

from conftest import FakeCommunicator, make_access, make_project, make_tasks
from sqlalchemy import select
from test_brief import CHAT, FakeAnthropic, add_msg, ev

from autopilot import roles
from autopilot.config import cfg
from autopilot.db import AccessItem, Project, Session, Task
from autopilot.fakes import FakeExecutor, FakeVerifier
from autopilot.planner import (CycleInPlan, Planner, autonomy, classify,
                               pick_executor, topological_order, valid_checks)
from autopilot.scheduler import Scheduler


def shell(cmd="npm run build"):
    return {"type": "shell", "cmd": cmd}


def task(title, ref="каталог с фильтрами", *, acceptance=None, verify="auto",
         executor="claude_code", depends=None, **extra):
    out = {
        "title": title, "description": f"описание: {title}",
        "deliverable_ref": ref,
        "acceptance": acceptance if acceptance is not None else [shell()],
        "verify_class": verify, "executor": executor,
        "depends_on": depends or [], "estimate_min": 60, "risk": "—",
    }
    out.update(extra)
    return out


def plan_reply(*tasks) -> str:
    return json.dumps({"tasks": list(tasks)}, ensure_ascii=False)


BRIEF = {
    "goal": {"text": "интернет-магазин"},
    "deliverables": [
        {"text": "каталог с фильтрами", "priority": "must"},
        {"text": "корзина и оформление заказа", "priority": "must"},
        {"text": "дизайн в фирменном стиле", "priority": "should"},
    ],
    "stack": [{"text": "WordPress"}],
    "constraints": [], "assets": [], "access_needed": [],
    "open_questions": [], "out_of_scope": [], "confidence": 0.9, "unreadable": [],
}


async def with_brief(brief=None, **kw) -> Project:
    p = await make_project(**kw)
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.brief = {"brief": brief or BRIEF, "_meta": {}}
        await s.commit()
        return await s.get(Project, p.id)


async def tasks_of(project_id: int) -> list[Task]:
    async with Session() as s:
        return (await s.execute(
            select(Task).where(Task.project_id == project_id, Task.lane == "build")
            .order_by(Task.order_idx))).scalars().all()


# ---------- происхождение задачи ----------

async def test_task_requires_deliverable_ref(db):
    """Задача без ссылки на пункт ТЗ отбрасывается."""
    p = await with_brief()
    fake = FakeAnthropic(plan_reply(
        task("Сверстать каталог", "каталог с фильтрами"),
        task("Прикрутить чат поддержки", "живой чат с оператором"),   # нет в ТЗ
        dict(task("Что-то ещё"), deliverable_ref=""),
    ))
    # третья задача не пройдёт схему -> повтор; отдадим второй раз без неё
    fake.replies.append(plan_reply(
        task("Сверстать каталог", "каталог с фильтрами"),
        task("Прикрутить чат поддержки", "живой чат с оператором")))

    result = await Planner(client=fake).plan(p)

    titles = [t["title"] for t in result["tasks"]]
    assert titles == ["Сверстать каталог"]
    assert any("не найден в ТЗ" in n for n in result["notes"])


# ---------- класс проверяемости ----------

def test_classify_by_checks():
    assert classify({"acceptance": [shell()]}) == "auto"
    assert classify({"acceptance": [{"type": "http", "url": "https://x"}]}) == "auto"
    assert classify({"acceptance": [{"type": "llm", "criteria": "красиво"}]}) == "assisted"
    assert classify({"acceptance": [{"type": "human", "criteria": "нравится"}]}) == "human"
    assert classify({"acceptance": []}) == "human"
    # проверка без обязательного поля не запустится, значит её нет
    assert classify({"acceptance": [{"type": "shell"}]}) == "human"
    assert valid_checks([{"type": "выдуманный", "cmd": "x"}]) == []


async def test_verify_class_downgraded(db):
    """Модель заявила auto, но детерминированной проверки нет — класс понижен."""
    p = await with_brief()
    fake = FakeAnthropic(plan_reply(
        task("Сделать красивый дизайн", "дизайн в фирменном стиле",
             acceptance=[{"type": "llm", "criteria": "выглядит аккуратно"}], verify="auto"),
        task("Собрать каталог", "каталог с фильтрами",
             acceptance=[{"type": "human", "criteria": "проверить глазами"}], verify="auto"),
    ))
    result = await Planner(client=fake).plan(p)

    by_title = {t["title"]: t for t in result["tasks"]}
    assert by_title["Сделать красивый дизайн"]["verify_class"] == "assisted"
    assert by_title["Собрать каталог"]["verify_class"] == "human"
    assert any("класс понижен" in n for n in result["notes"])


async def test_human_task_never_builds(db):
    """Задача класса human не получает build-слот ни при каких условиях."""
    p = await make_project()
    await make_tasks(p.id, 2, verify_class="human", executor="claude_code")
    await make_tasks(p.id, 1, start_idx=10, verify_class="auto", executor="claude_code")

    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    for _ in range(3):
        await sched.tick()
        await sched.drain()

    assert len(ex.calls) == 1, "в работу ушла задача, которую нечем проверить"


async def test_executor_manual_not_built(db):
    """Задача с executor=manual не идёт в build."""
    p = await make_project()
    await make_tasks(p.id, 2, verify_class="auto", executor="manual")
    await make_tasks(p.id, 1, start_idx=10, verify_class="auto", executor="external")

    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    for _ in range(3):
        await sched.tick()
        await sched.drain()

    assert ex.calls == [], "агенту отдали работу в чужой админке"


def test_pick_executor_downgrades():
    """Модель считает, что агент может всё. Проверяем по признакам."""
    assert pick_executor(
        {"title": "Установить плагин WPML через админку", "acceptance": [shell()]},
        "claude_code") == "manual"
    assert pick_executor(
        {"title": "Настроить счётчик метрики в кабинете", "acceptance": [shell()]},
        "claude_code") == "manual"
    assert pick_executor(
        {"title": "Сверстать шаблон карточки товара", "acceptance": [shell()]},
        "claude_code") == "claude_code"
    # понизить модель вправе, повысить — нет
    assert pick_executor({"title": "что угодно", "acceptance": [shell()]},
                         "manual") == "manual"


# ---------- зависимости ----------

async def test_depends_on_blocks(db):
    """Задача не выдаётся, пока зависимость не done."""
    p = await make_project()
    async with Session() as s:
        base = Task(project_id=p.id, lane="build", status="ready", order_idx=0,
                    title="каркас темы", verify_class="auto", executor="claude_code",
                    depends_on=[])
        dependent = Task(project_id=p.id, lane="build", status="ready", order_idx=1,
                         title="карточка товара", verify_class="auto",
                         executor="claude_code", depends_on=["каркас темы"])
        s.add_all([base, dependent])
        await s.commit()
        base_id = base.id

    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()

    assert [t for _, t in ex.calls] == [base_id], "зависимая задача поехала раньше времени"

    # закрываем зависимость
    async with Session() as s:
        row = await s.get(Task, base_id)
        row.status, row.lane = "done", "build"
        await s.commit()

    await sched.tick()
    await sched.drain()
    assert len(ex.calls) == 2, "после закрытия зависимости задача так и не поехала"


def test_cycle_detected():
    """Циклический граф даёт исключение, а не зависание."""
    tasks = [
        {"title": "A", "depends_on": ["C"]},
        {"title": "B", "depends_on": ["A"]},
        {"title": "C", "depends_on": ["B"]},
    ]
    try:
        topological_order(tasks)
    except CycleInPlan as e:
        assert "цикл" in str(e)
    else:
        raise AssertionError("цикл не пойман")

    # нормальный граф упорядочивается
    ok = [{"title": "A", "depends_on": []}, {"title": "B", "depends_on": ["A"]}]
    assert topological_order(ok) == ["A", "B"]


async def test_cycle_escalates(db):
    """Цикл в плане — эскалация владельцу, а не молчаливая попытка разрулить."""
    p = await with_brief()
    comm = FakeCommunicator()
    fake = FakeAnthropic(plan_reply(
        task("A", depends=["B"]),
        task("B", "корзина и оформление заказа", depends=["A"]),
    ))
    assert await Planner(client=fake, communicator=comm).plan(p) is None

    async with Session() as s:
        fresh = await s.get(Project, p.id)
    assert fresh.status == "blocked"
    assert any("цикл" in n for n in comm.owner_notes)


# ---------- автономность ----------

def test_autonomy_ratio():
    """Проект с преобладанием human-задач помечается малопригодным."""
    good = [{"verify_class": "auto", "executor": "claude_code"} for _ in range(4)]
    good += [{"verify_class": "human", "executor": "manual"}]
    stats = autonomy(good)
    assert stats["auto_ratio"] == 0.8 and stats["suitable"] is True

    bad = [{"verify_class": "human", "executor": "manual"} for _ in range(4)]
    bad += [{"verify_class": "auto", "executor": "claude_code"}]
    stats = autonomy(bad)
    assert stats["auto_ratio"] == 0.2 and stats["suitable"] is False
    assert stats["by_class"]["human"] == 4


async def test_autonomy_written_and_reported(db):
    """Цифра автономности попадает в проект и уходит владельцу до начала работы."""
    p = await with_brief()
    comm = FakeCommunicator()
    fake = FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами"),
        task("Дизайн", "дизайн в фирменном стиле",
             acceptance=[{"type": "human", "criteria": "нравится"}], verify="human",
             executor="manual"),
        task("Корзина", "корзина и оформление заказа",
             acceptance=[{"type": "human", "criteria": "проверить"}], verify="human",
             executor="manual"),
    ))
    result = await Planner(client=fake, communicator=comm).plan(p)

    assert result["stats"]["auto_ratio"] < cfg.autonomy_min_ratio
    async with Session() as s:
        fresh = await s.get(Project, p.id)
    assert fresh.autonomy_ratio == result["stats"]["auto_ratio"]
    assert any("автономно" in n for n in comm.owner_notes)


# ---------- перепланирование ----------

async def test_replan_keeps_done(db):
    """Перепланирование не трогает выполненные задачи."""
    p = await with_brief()
    fake = FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами"),
        task("Корзина", "корзина и оформление заказа")))
    await Planner(client=fake).plan(p)

    async with Session() as s:
        row = (await s.execute(select(Task).where(Task.title == "Каталог"))).scalars().one()
        row.status = "done"
        row.prompt = "исходный текст, который нельзя перезаписывать"
        await s.commit()
        done_id = row.id

    again = FakeAnthropic(plan_reply(
        dict(task("Каталог", "каталог с фильтрами"), description="ПЕРЕПИСАННОЕ описание"),
        task("Корзина", "корзина и оформление заказа")))
    await Planner(client=again).plan(await _reload(p))

    async with Session() as s:
        row = await s.get(Task, done_id)
    assert row.status == "done"
    assert row.prompt == "исходный текст, который нельзя перезаписывать"


async def test_orphaned_task(db):
    """Задача, чьё требование исчезло из брифа, помечается orphaned."""
    p = await with_brief()
    fake = FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами"),
        task("Корзина", "корзина и оформление заказа")))
    await Planner(client=fake).plan(p)
    assert len(await tasks_of(p.id)) == 2

    # в новом плане корзины нет
    again = FakeAnthropic(plan_reply(task("Каталог", "каталог с фильтрами")))
    await Planner(client=again).plan(await _reload(p))

    rows = {t.title: t for t in await tasks_of(p.id)}
    assert len(rows) == 2, "задача удалена молча"
    assert rows["Корзина"].orphaned is True
    assert rows["Каталог"].orphaned is False

    # и в работу осиротевшая не идёт
    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()
    assert all(t != rows["Корзина"].id for _, t in ex.calls)


# ---------- доступы ----------

async def test_no_duplicate_access_tasks(db):
    """Доступы ведёт чеклист, задачами они не дублируются."""
    p = await with_brief()
    await make_access(p.id, "Панель Beget", "hosting_panel", "needed")

    fake = FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами"),
        task("Получить доступ к панели хостинга", "каталог с фильтрами",
             acceptance=[{"type": "human", "criteria": "клиент прислал"}],
             verify="human", executor="external"),
    ))
    result = await Planner(client=fake).plan(p)

    titles = [t["title"] for t in result["tasks"]]
    assert titles == ["Каталог"]
    assert any("чеклиста доступов" in n for n in result["notes"])


# ---------- блокирующие вопросы ----------

async def test_blocking_questions_only(db):
    """Неблокирующий вопрос не держит проект."""
    from autopilot.brief import Brief, blocking_questions
    from test_brief import reply as brief_reply

    p = await make_project()
    await add_msg(p.id, "1", "нужен каталог с фильтрами", roles.CLIENT)

    soft = brief_reply(
        confidence=0.9,
        deliverables=[{"text": "каталог", "evidence": ev("1"), "priority": "must"}],
        open_questions=[{"text": "какие именно типы кожи в фильтре?",
                         "evidence": ev("1"), "blocking": False}])
    await Brief(client=FakeAnthropic(soft)).build(p)

    fresh = await _reload(p)
    assert fresh.brief_ready is True, "уточнение по ходу дела не должно держать проект"
    data = fresh.brief["brief"]
    assert blocking_questions(data) == []

    # а блокирующий — держит
    await add_msg(p.id, "2", "и оплата картой", roles.CLIENT)
    hard = brief_reply(
        confidence=0.9,
        deliverables=[{"text": "каталог", "evidence": ev("1"), "priority": "must"}],
        open_questions=[{"text": "какой платёжный шлюз использовать?",
                         "evidence": ev("2"), "blocking": True}])
    await Brief(client=FakeAnthropic(hard)).build(fresh)

    fresh = await _reload(p)
    assert fresh.brief_ready is False
    assert len(blocking_questions(fresh.brief["brief"])) == 1


async def test_questions_trimmed(db):
    """Объединение прогонов не должно давать список из четырнадцати вопросов."""
    from autopilot.brief import trim_questions

    data = {"open_questions": [
        {"text": f"вопрос {i}", "blocking": i == 9} for i in range(14)]}
    trimmed = trim_questions(data)["open_questions"]
    assert len(trimmed) == cfg.brief_max_questions
    assert trimmed[0]["text"] == "вопрос 9", "блокирующий должен идти первым"


async def _reload(p: Project) -> Project:
    async with Session() as s:
        return await s.get(Project, p.id)


def test_class_is_never_raised():
    """Код умеет только понижать класс. Регрессия: на живом прогоне модель
    честно сказала human про вёрстку по макету, а код поднял до auto из-за
    прицепленного http 200 — и план вышел «100% авто»."""
    honest_human = {"acceptance": [{"type": "http", "url": "https://site/"}]}
    assert classify(honest_human, "human") == "human"
    assert classify(honest_human, "assisted") == "assisted"
    assert classify(honest_human, "auto") == "auto"

    # human-критерий в списке сам по себе не даёт машине последнего слова
    mixed = {"acceptance": [shell(), {"type": "human", "criteria": "нравится"}]}
    assert classify(mixed, "auto") == "human"


async def test_honest_human_task_survives_planning(db):
    """Честное «это глазами» доживает до плана и не идёт в build."""
    p = await with_brief()
    fake = FakeAnthropic(plan_reply(
        task("Вёрстка по макету", "дизайн в фирменном стиле",
             acceptance=[{"type": "http", "url": "https://site/"}], verify="human"),
        task("Сборка темы", "каталог с фильтрами", verify="auto"),
    ))
    result = await Planner(client=fake).plan(p)

    by_title = {t["title"]: t for t in result["tasks"]}
    assert by_title["Вёрстка по макету"]["verify_class"] == "human"
    assert by_title["Сборка темы"]["verify_class"] == "auto"
    assert result["stats"]["by_class"]["human"] == 1

    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()
    async with Session() as s:
        built = {(await s.get(Task, tid)).title for _, tid in ex.calls}
    assert "Вёрстка по макету" not in built


async def test_replan_does_not_breed_orphans_on_rewording(db):
    """Переформулированное название — та же задача, а не новая плюс сирота.

    Сопоставление шло по ТОЧНОМУ названию, а модель каждый раз пишет иначе.
    Каждый прогон заводил план заново и осиротял предыдущий целиком: три
    прогона по проекту 8 дали 84 сироты при 26 живых задачах.
    """
    p = await with_brief()
    await Planner(client=FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами"),
        task("Корзина", "корзина и оформление заказа")))).plan(p)
    first = {t.title: t.id for t in await tasks_of(p.id)}
    assert len(first) == 2

    # тот же план, но названия переписаны — работа не изменилась
    await Planner(client=FakeAnthropic(plan_reply(
        task("Каталог товаров с фильтрами", "каталог с фильтрами"),
        task("Корзина и оформление заказа", "корзина и оформление заказа")))).plan(
            await _reload(p))

    rows = await tasks_of(p.id)
    assert len(rows) == 2, f"план размножился: {[t.title for t in rows]}"
    assert not any(t.orphaned for t in rows), "живые задачи объявлены сиротами"
    # это те же строки, а не новые: id сохранились
    assert {t.id for t in rows} == set(first.values())


async def test_replan_does_not_glue_different_tasks_of_one_requirement(db):
    """Из одного пункта ТЗ выходит несколько РАЗНЫХ задач — склеивать нельзя.

    Ошибка в эту сторону дороже сироты: склеенная задача молча исчезает
    из плана, и никто этого не замечает.
    """
    p = await with_brief()
    await Planner(client=FakeAnthropic(plan_reply(
        task("Каталог: вёрстка списка", "каталог с фильтрами"),
        task("Каталог: фильтры по бренду", "каталог с фильтрами")))).plan(p)
    assert len(await tasks_of(p.id)) == 2

    await Planner(client=FakeAnthropic(plan_reply(
        task("Каталог: вёрстка списка", "каталог с фильтрами"),
        task("Каталог: фильтры по бренду", "каталог с фильтрами")))).plan(
            await _reload(p))

    rows = await tasks_of(p.id)
    assert len(rows) == 2, "две разные задачи схлопнулись в одну"
    assert not any(t.orphaned for t in rows)


async def test_task_from_another_requirement_is_not_matched(db):
    """Похожее название при ДРУГОМ пункте ТЗ — это другая работа."""
    from autopilot.planner import match_task

    class Row:
        def __init__(self, id, title, ref):
            self.id, self.title, self.deliverable_ref = id, title, ref

    rows = [Row(1, "Импорт товаров", "каталог с фильтрами")]
    same = match_task({"title": "Импорт товаров в каталог",
                       "deliverable_ref": "каталог с фильтрами"}, rows, set())
    assert same is not None and same.id == 1

    other = match_task({"title": "Импорт товаров",
                        "deliverable_ref": "корзина и оформление заказа"}, rows, set())
    assert other is None, "задача подхвачена из чужого пункта ТЗ"

    # уже занятую строку второй раз не отдаём
    assert match_task({"title": "Импорт товаров",
                       "deliverable_ref": "каталог с фильтрами"}, rows, {1}) is None


# ---------- уборка сирот ----------

async def test_prune_removes_only_untouched_orphans(db):
    """Удаляем след планировщика, но не следы работы."""
    from autopilot.db import Run
    from autopilot.manual import NEEDS_HUMAN, prunable, prune

    p = await with_brief()
    await Planner(client=FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами"),
        task("Корзина", "корзина и оформление заказа")))).plan(p)

    async with Session() as s:
        rows = {t.title: t for t in
                (await s.execute(select(Task).where(Task.project_id == p.id,
                                                    Task.lane == "build"))).scalars()}
        # чистая сирота: план передумал, работы по ней не было
        rows["Каталог"].orphaned = True
        # сирота с историей: её делали и она упёрлась в человека
        rows["Корзина"].orphaned = True
        rows["Корзина"].attempts = 2
        rows["Корзина"].status = NEEDS_HUMAN
        s.add(Run(task_id=rows["Корзина"].id, kind="execute", ok=True, backend="cli"))
        await s.commit()
        clean_id, worked_id = rows["Каталог"].id, rows["Корзина"].id

    safe, keep = await prunable(p.id)
    assert [t.id for t in safe] == [clean_id]
    assert [t.id for t in keep] == [worked_id]

    # примерка ничего не трогает
    await prune(p.id, apply=False)
    async with Session() as s:
        assert await s.get(Task, clean_id) is not None

    removed, kept = await prune(p.id, apply=True)
    assert (removed, kept) == (1, 1)
    async with Session() as s:
        assert await s.get(Task, clean_id) is None, "чистая сирота осталась"
        assert await s.get(Task, worked_id) is not None, "стёрт след настоящей работы"


async def test_prune_leaves_live_tasks_alone(db):
    """Живые задачи уборка не видит вовсе."""
    from autopilot.manual import prunable

    p = await with_brief()
    await Planner(client=FakeAnthropic(plan_reply(
        task("Каталог", "каталог с фильтрами")))).plan(p)

    safe, keep = await prunable(p.id)
    assert (safe, keep) == ([], [])
