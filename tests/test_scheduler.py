"""Планировщик: честность WFQ, приоритеты, эксклюзивность полос, бюджет."""
from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter

import pytest
from sqlalchemy import select
from conftest import FakeCommunicator, make_access, make_project, make_tasks

from autopilot.config import cfg
from autopilot.db import AccessItem, Project, Run, Session, Task
from autopilot.fakes import FakeExecutor, FakeVerifier
from autopilot.scheduler import Scheduler, weight


def make_sched(communicator=None, executor=None, verifier=None):
    return Scheduler(executor or FakeExecutor(),
                     verifier or FakeVerifier(),
                     communicator or FakeCommunicator())


async def test_scheduler_fairness(db):
    """Проект с 40 задачами не должен съесть полосу: каждый из пяти проектов
    обязан получить слот в первые 10 тиков."""
    projects = [await make_project(title=f"p{i}") for i in range(5)]
    await make_tasks(projects[0].id, 40)
    for p in projects[1:]:
        await make_tasks(p.id, 2)

    ex = FakeExecutor()
    sched = make_sched(executor=ex)

    served_by_tick: list[set[int]] = []
    for _ in range(10):
        before = len(ex.served)
        await sched.tick()
        await sched.drain()
        served_by_tick.append(set(ex.served[before:]))

    got_slot = set(ex.served)
    missed = {p.id for p in projects} - got_slot
    assert not missed, f"за 10 тиков не обслужены проекты: {missed} (порядок: {served_by_tick})"

    # пока работа есть у всех, круг проходится целиком: первые пять выданных
    # слотов достаются пяти разным проектам, а не жадному по кругу
    assert len(set(ex.served[:5])) == 5, f"первые пять слотов: {ex.served[:5]}"
    # пока у мелких проектов есть работа, жадный не получает больше слотов,
    # чем все остальные вместе. Дальше он забирает всё — но это уже не
    # несправедливость, а отсутствие конкурентов
    greedy_id = projects[0].id
    last_rival = max(i for i, pid in enumerate(ex.served) if pid != greedy_id)
    contested = ex.served[:last_rival + 1]
    greedy = contested.count(greedy_id)
    assert greedy <= len(contested) - greedy, (
        f"в конкурентной фазе жадный забрал {greedy} из {len(contested)}: {contested}")


async def test_scheduler_priority(db):
    """Приоритет 1 + просроченный дедлайн обслуживается заметно чаще фонового.

    Полоса chat: она не эксклюзивная, поэтому видно именно распределение весов,
    а не потолок «один слот на проект»."""
    hot = await make_project(title="горит", priority=1,
                             deadline=dt.date.today() - dt.timedelta(days=1))
    bg = await make_project(title="фон", priority=3, deadline=None)
    await make_tasks(hot.id, 120, lane="chat")
    await make_tasks(bg.id, 120, lane="chat")

    comm = FakeCommunicator()
    sched = make_sched(communicator=comm)

    for _ in range(10):
        await sched.tick()
        await sched.drain()

    hot_n = comm.processed.count(hot.id)
    bg_n = comm.processed.count(bg.id)
    assert hot_n + bg_n > 0
    # веса 3*4=12 против 1 — на дистанции разрыв должен быть кратным
    assert hot_n >= 3 * max(bg_n, 1), f"горящий={hot_n}, фоновый={bg_n}"


async def test_no_double_build(db):
    """Один проект не занимает два build-слота одновременно."""
    p = await make_project()
    await make_tasks(p.id, 5)

    peak = {"value": 0}
    live: set[int] = set()

    class Probe:
        async def run(self, task, project):
            live.add(task.id)
            peak["value"] = max(peak["value"], len(live))
            try:
                await asyncio.sleep(0.15)
            finally:
                live.discard(task.id)
            return {}

    sched = make_sched(executor=Probe())

    for _ in range(3):
        await sched.tick()
        # даём воркерам стартовать, но не дожидаемся их — именно в этот момент
        # и ловится вторая параллельная сборка одного проекта
        await asyncio.sleep(0.05)
        assert sched.running["build"] <= 1
        assert sched.busy_projects <= {p.id}
    await sched.drain()

    assert peak["value"] == 1, f"проект держал {peak['value']} build-слотов разом"


async def test_budget_guard(db, monkeypatch):
    """Превышен DAILY_BUDGET_USD — build и verify встают, chat работает."""
    p = await make_project()
    build_ids = await make_tasks(p.id, 3, lane="build")
    await make_tasks(p.id, 3, lane="verify", start_idx=10)
    await make_tasks(p.id, 3, lane="chat", start_idx=20)

    monkeypatch.setattr(cfg, "daily_budget_usd", 1.0)
    async with Session() as s:
        s.add(Run(task_id=build_ids[0], kind="execute", ok=True, cost_usd=5.0))
        await s.commit()

    ex, ver, comm = FakeExecutor(), FakeVerifier(), FakeCommunicator()
    sched = make_sched(communicator=comm, executor=ex, verifier=ver)

    for _ in range(3):
        await sched.tick()
        await sched.drain()

    assert sched.budget_paused is True
    assert ex.calls == [], "build поехал при исчерпанном бюджете"
    assert ver.calls == [], "verify поехал при исчерпанном бюджете"
    assert comm.processed, "chat должен продолжать работать"

    # вернули бюджет — полосы разморозились
    monkeypatch.setattr(cfg, "daily_budget_usd", 1000.0)
    await sched.tick()
    await sched.drain()
    assert sched.budget_paused is False
    assert ex.calls, "после возврата бюджета build не поехал"


async def test_failed_chat_task_stays_in_chat(db):
    """Упавшая chat-задача не должна переезжать в build: там у неё нет смысла."""
    p = await make_project()
    (task_id,) = await make_tasks(p.id, 1, lane="chat")

    class Boom(FakeCommunicator):
        async def process(self, task, project):
            raise RuntimeError("бум")

    sched = make_sched(communicator=Boom())
    await sched.tick()
    await sched.drain()

    async with Session() as s:
        t = await s.get(Task, task_id)
    assert t.lane == "chat"
    assert t.status == "ready"
    assert t.attempts == 1


async def test_project_status_flow(db):
    """briefing -> active при первом build-слоте, active -> review когда всё закрыто."""
    p = await make_project(status="briefing")
    await make_tasks(p.id, 1, lane="build")

    sched = make_sched()
    await sched.tick()           # build
    await sched.drain()

    async with Session() as s:
        proj = await s.get(type(p), p.id)
    assert proj.status == "active"

    await sched.tick()           # verify
    await sched.drain()

    async with Session() as s:
        proj = await s.get(type(p), p.id)
    assert proj.status == "review"


@pytest.mark.parametrize("status", ["blocked", "done"])
async def test_dead_projects_are_not_served(db, status):
    p = await make_project(status=status)
    await make_tasks(p.id, 3)

    ex = FakeExecutor()
    sched = make_sched(executor=ex)
    await sched.tick()
    await sched.drain()

    assert ex.calls == []


async def test_access_blocks_build(db):
    """Пока хоть один пункт чеклиста не verified, build-слот не выдаётся."""
    p = await make_project(status="active")
    await make_tasks(p.id, 3, lane="build")
    await make_tasks(p.id, 1, lane="chat", start_idx=50)
    await make_access(p.id, "FTP хостинга", "ftp", "verified")
    item = await make_access(p.id, "Панель хостинга", "hosting_panel", "requested")

    ex, comm = FakeExecutor(), FakeCommunicator()
    sched = make_sched(communicator=comm, executor=ex)

    for _ in range(3):
        await sched.tick()
        await sched.drain()

    assert ex.calls == [], "проект без доступов получил build-слот"
    async with Session() as s:
        proj = await s.get(Project, p.id)
    assert proj.status == "blocked_access"
    assert comm.processed, "переписка в chat должна идти и без доступов"
    assert comm.reminders, "клиенту не напомнили про недостающий доступ"

    # доступ пришёл и проверен — полоса открывается
    async with Session() as s:
        stored = await s.get(AccessItem, item.id)
        stored.status = "verified"
        await s.commit()

    await sched.tick()
    await sched.drain()

    assert ex.calls, "после verified проект так и не поехал"
    async with Session() as s:
        proj = await s.get(Project, p.id)
    assert proj.status == "active"


async def test_access_reminder_is_one_per_tick_round(db):
    """Планировщик зовёт напоминание с ПОЛНЫМ списком, а не по пункту."""
    p = await make_project(status="active")
    await make_tasks(p.id, 1, lane="build")
    for name in ("FTP", "Панель", "Домен"):
        await make_access(p.id, name, "other", "needed")

    comm = FakeCommunicator()
    sched = make_sched(communicator=comm)
    await sched.tick()
    await sched.drain()

    assert comm.reminders == [(p.id, 3)], f"вызовы напоминаний: {comm.reminders}"


async def test_sheet_gate(db):
    """Строка без галочки «Готов к работе» не отдаёт задач в build."""
    p = await make_project(status="briefing", ready_for_work=False)
    await make_tasks(p.id, 3, lane="build")

    ex = FakeExecutor()
    sched = make_sched(executor=ex)
    for _ in range(3):
        await sched.tick()
        await sched.drain()

    assert ex.calls == [], "проект без галочки менеджера поехал в работу"
    async with Session() as s:
        proj = await s.get(Project, p.id)
    assert proj.status == "briefing", "без галочки проект не выходит из briefing"

    async with Session() as s:
        proj = await s.get(Project, p.id)
        proj.ready_for_work = True
        await s.commit()

    await sched.tick()
    await sched.drain()

    assert ex.calls, "галочку поставили, а проект не поехал"
    async with Session() as s:
        proj = await s.get(Project, p.id)
    assert proj.status == "active"


async def test_served_units_decay(db, monkeypatch):
    """Простоявший проект перестаёт быть вечным должником очереди."""
    from autopilot.db import decayed_units, utcnow

    monkeypatch.setattr(cfg, "served_halflife_h", 24.0)
    now = utcnow()
    assert decayed_units(100.0, now, now) == pytest.approx(100.0)
    assert decayed_units(100.0, now - dt.timedelta(hours=24), now) == pytest.approx(50.0)
    assert decayed_units(100.0, now - dt.timedelta(hours=48), now) == pytest.approx(25.0)

    # и планировщик реально пользуется затухшим значением
    old = await make_project(title="давно ждёт")
    fresh = await make_project(title="новичок")
    await make_tasks(old.id, 5, lane="chat")
    await make_tasks(fresh.id, 5, lane="chat")
    async with Session() as s:
        p_old = await s.get(Project, old.id)
        p_old.served_units = 100.0
        p_old.served_at = utcnow() - dt.timedelta(hours=24 * 10)   # десять полураспадов
        await s.commit()

    comm = FakeCommunicator()
    sched = make_sched(communicator=comm)
    await sched.tick()
    await sched.drain()

    assert old.id in comm.processed, "проект с истлевшим долгом всё ещё в хвосте очереди"


async def test_wfq_without_any_deadlines(db):
    """Все сроки пустые — планировщик работает на базовом весе, а не падает.

    В рабочей таблице колонка «Срок» не заполнена ни у одного из 38 заказов,
    так что это не гипотетический случай, а ровно то состояние, в котором
    система пойдёт в первый живой прогон. Буст по дедлайну при этом не
    применяется никогда, и вес определяется только приоритетом.
    """
    p1 = await make_project(title="высокий", priority=1, deadline=None)
    p2 = await make_project(title="обычный", priority=2, deadline=None)
    p3 = await make_project(title="фоновый", priority=3, deadline=None)

    # вес считается и без дедлайна, ничего не роняя
    async with Session() as s:
        rows = {p.title: p for p in (await s.execute(select(Project))).scalars().all()}
    assert weight(rows["высокий"]) == 3.0
    assert weight(rows["обычный"]) == 2.0
    assert weight(rows["фоновый"]) == 1.0

    for proj in (p1, p2, p3):
        await make_tasks(proj.id, 60, lane="chat")

    ex, ver, comm = FakeExecutor(), FakeVerifier(), FakeCommunicator()
    sched = make_sched(comm, ex, ver)
    for _ in range(12):
        await sched.tick()
        await sched.drain()

    served = Counter(ex.served) + Counter(comm.processed)
    # каждый проект обслужен — никто не заперт из-за отсутствия срока
    for proj in (p1, p2, p3):
        assert served[proj.id] > 0, f"проект {proj.title} не получил ни одного слота"
    # и приоритет по-прежнему решает: высокий обслужен чаще фонового
    assert served[p1.id] > served[p3.id], (
        f"приоритет перестал работать без дедлайнов: {dict(served)}")


async def test_wfq_deadline_boost_still_applies_when_filled(db):
    """Обратная сторона: как только «Срок» заполнен, буст возвращается."""
    overdue = await make_project(title="просрочен", priority=2,
                                 deadline=dt.date.today() - dt.timedelta(days=1))
    soon = await make_project(title="скоро", priority=2,
                              deadline=dt.date.today() + dt.timedelta(days=2))
    far = await make_project(title="не горит", priority=2,
                             deadline=dt.date.today() + dt.timedelta(days=90))
    async with Session() as s:
        rows = {p.title: p for p in (await s.execute(select(Project))).scalars().all()}
    assert weight(rows["просрочен"]) == 8.0     # 2.0 x 4
    assert weight(rows["скоро"]) == 4.0         # 2.0 x 2
    assert weight(rows["не горит"]) == 2.0      # без буста
