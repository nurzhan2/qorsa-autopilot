"""Цикл целиком: журнал попыток, застревание, сигнал человеку, бесплатная приёмка.

Отдельный файл от `test_scheduler.py` намеренно: там про честность WFQ
и полосы, здесь — про то, как цикл выходит из тупика и что он при этом
рассказывает человеку.

Первый тест файла — инвариант, и он важнее остальных: **ни один новый
механизм не может объявить задачу выполненной.** Это право только
у детерминированных проверок верификатора. Тот же приём, что
у `test_diagnosis_never_marks_a_task_done` в `test_crew.py`.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from conftest import FakeCommunicator, make_project, make_tasks

from autopilot import breaker, worktrace
from autopilot.config import cfg
from autopilot.db import (Run, Session, Task, TaskAttempt, engine,
                          log_attempt)
from autopilot.fakes import FakeExecutor, FakeVerifier
from autopilot.manual import NEEDS_HUMAN
from autopilot.scheduler import Scheduler
from autopilot.verifier import Verdict


def make_sched(communicator=None, executor=None, verifier=None):
    return Scheduler(executor or FakeExecutor(),
                     verifier or FakeVerifier(),
                     communicator or FakeCommunicator())


class FailingVerifier:
    """Приёмка, которая всегда возвращает один и тот же дефект."""

    def __init__(self, defect="GET /admin/ -> 200, ожидалось 302"):
        self.defect = defect
        self.calls: list[int] = []

    async def run(self, task, project):
        self.calls.append(task.id)
        return Verdict(ok=False, defects=[self.defect], deterministic=1)


async def drive(sched, ticks: int = 12) -> None:
    for _ in range(ticks):
        await sched.tick()
        await sched.drain()


# ---------- инвариант ----------

async def test_no_new_mechanism_closes_a_task(db):
    """Журнал, детектор застревания и политика умеют только НЕ ДАТЬ закрыть.

    Проверяется на самом дорогом пути: задача проваливается снова и снова,
    цикл её останавливает — и ни разу не объявляет сделанной.
    """
    p = await make_project()
    [task_id] = await make_tasks(p.id, 1, lane="verify")
    ver = FailingVerifier()
    sched = make_sched(verifier=ver)
    await drive(sched)

    async with Session() as s:
        status = (await s.get(Task, task_id)).status
    assert status != "done", "механизм цикла закрыл задачу — этого права у него нет"


# ---------- журнал попыток ----------

async def test_every_failure_leaves_a_row(db):
    p = await make_project()
    [task_id] = await make_tasks(p.id, 1, lane="verify")
    sched = make_sched(verifier=FailingVerifier())
    await drive(sched, ticks=2)

    async with Session() as s:
        rows = (await s.execute(
            select(TaskAttempt).where(TaskAttempt.task_id == task_id)
            .order_by(TaskAttempt.n))).scalars().all()
    assert rows and rows[0].outcome == "failed"
    assert rows[0].counted is True
    assert rows[0].sig == breaker.signature([rows[0].defects[0]])


async def test_ledger_numbering_is_dense(db):
    """Номер считается по самому журналу, а не по `Task.attempts`: исходы,
    не тратящие попытку, тоже оставляют строку."""
    p = await make_project()
    [task_id] = await make_tasks(p.id, 1)
    for _ in range(3):
        await log_attempt(task_id, project_id=p.id, outcome="failed")
    async with Session() as s:
        ns = [r.n for r in (await s.execute(
            select(TaskAttempt).where(TaskAttempt.task_id == task_id)
            .order_by(TaskAttempt.n))).scalars().all()]
    assert ns == [1, 2, 3]


# ---------- застревание ----------

async def test_same_defect_three_times_stops_the_loop(db, monkeypatch):
    """Случай 531 дословно: четыре одинаковых провала подряд.

    Раньше цикл честно доходил до MAX_ATTEMPTS и писал «исчерпаны попытки» —
    формулировка, из которой не следует ничего. Теперь он останавливается
    раньше и называет причину: дефект тот же, проверь выполнимость критерия.
    """
    monkeypatch.setattr(cfg, "max_attempts", 8)     # чтобы сработала не эскалация
    p = await make_project()
    [task_id] = await make_tasks(p.id, 1, lane="verify")
    comm = FakeCommunicator()
    ver = FailingVerifier()
    sched = make_sched(communicator=comm, verifier=ver)
    await drive(sched)

    async with Session() as s:
        row = await s.get(Task, task_id)
    assert row.status == NEEDS_HUMAN
    assert "один и тот же дефект" in row.last_error
    assert "критерий" in row.last_error.lower()
    assert len(ver.calls) == cfg.breaker_stagnation, (
        "цикл должен встать ровно на пороге застревания, а не позже")
    assert any("топчется" in n for n in comm.owner_notes)


async def test_moving_task_is_not_called_stuck(db, monkeypatch):
    """РАЗНЫЕ дефекты — это работа, а не буксование: агент чинит по одному.
    Ложное срабатывание тут снижает автономность вместо того, чтобы поднять."""
    monkeypatch.setattr(cfg, "max_attempts", 8)

    class Moving:
        def __init__(self):
            self.n = 0

        async def run(self, task, project):
            self.n += 1
            return Verdict(ok=False, defects=[f"дефект номер {self.n} совсем другой"],
                           deterministic=1)

    p = await make_project()
    [task_id] = await make_tasks(p.id, 1, lane="verify")
    sched = make_sched(verifier=Moving())
    await drive(sched, ticks=5)

    async with Session() as s:
        row = await s.get(Task, task_id)
    assert row.status != NEEDS_HUMAN
    assert row.attempts >= 3


async def test_stagnation_does_not_replace_escalation(db, monkeypatch):
    """Когда попытки кончились, работает обычная эскалация: заводить для неё
    второе имя незачем."""
    monkeypatch.setattr(cfg, "breaker_stagnation", 0)   # застревание выключено
    monkeypatch.setattr(cfg, "max_attempts", 2)
    p = await make_project()
    [task_id] = await make_tasks(p.id, 1, lane="verify")
    comm = FakeCommunicator()
    sched = make_sched(communicator=comm, verifier=FailingVerifier())
    await drive(sched)

    async with Session() as s:
        row = await s.get(Task, task_id)
    assert row.status == "escalated"
    assert any("эскалации" in n for n in comm.owner_notes)


# ---------- сигнал человеку ----------

async def test_needs_human_is_announced(db):
    """Самый дорогой исход цикла был единственным без сигнала владельцу:
    обе ветки только писали в лог, при том что менее срочное «нужно
    подтверждение» уведомление слало."""

    class Unrunnable:
        async def run(self, task, project):
            return Verdict(ok=False,
                           defects=["нет ни одной исполнимой проверки — "
                                    "принять задачу нечем"])

    p = await make_project()
    await make_tasks(p.id, 1, lane="verify")
    comm = FakeCommunicator()
    sched = make_sched(communicator=comm, verifier=Unrunnable())
    await drive(sched, ticks=2)

    assert any("ждёт тебя" in n for n in comm.owner_notes)


# ---------- приёмка без модели ----------

DETERMINISTIC = [{"type": "shell", "cmd": "pytest tests/test_cart.py"}]
ASSISTED = [{"type": "llm", "criteria": "выглядит ли корзина как в макете"}]


async def set_acceptance(task_id: int, acceptance) -> None:
    async with Session() as s:
        row = await s.get(Task, task_id)
        row.acceptance = acceptance
        await s.commit()


async def test_free_verification_survives_an_exhausted_budget(db, monkeypatch):
    """Задача с одними детерминированными критериями к модели не ходит вовсе:
    судья зовётся только из `llm` и `screenshot`. Раньше такая приёмка вставала
    вместе со сборкой, и собранная работа копилась в очереди до завтра
    из-за расхода, которого не будет."""
    p = await make_project()
    [free_id] = await make_tasks(p.id, 1, lane="verify")
    [paid_id] = await make_tasks(p.id, 1, lane="verify", start_idx=5)
    await set_acceptance(free_id, DETERMINISTIC)
    await set_acceptance(paid_id, ASSISTED)

    monkeypatch.setattr(cfg, "daily_budget_usd", 1.0)
    async with Session() as s:
        s.add(Run(task_id=free_id, kind="execute", ok=True, backend="api",
                  cost_usd=5.0))
        await s.commit()

    ver = FakeVerifier()
    sched = make_sched(verifier=ver)
    await drive(sched, ticks=4)

    assert sched.budget_paused is True
    assert free_id in ver.calls, "бесплатная приёмка встала из-за чужого расхода"
    assert paid_id not in ver.calls, "приёмка с моделью поехала при пустом бюджете"


async def test_build_still_stops_on_budget(db, monkeypatch):
    """Послабление касается ТОЛЬКО приёмки без модели. Сборка ходит к модели
    всегда, и гасить её надо по-прежнему."""
    p = await make_project()
    [build_id] = await make_tasks(p.id, 1, lane="build")
    await set_acceptance(build_id, DETERMINISTIC)

    monkeypatch.setattr(cfg, "daily_budget_usd", 1.0)
    async with Session() as s:
        s.add(Run(task_id=build_id, kind="execute", ok=True, backend="api",
                  cost_usd=5.0))
        await s.commit()

    ex = FakeExecutor()
    sched = make_sched(executor=ex)
    await drive(sched, ticks=4)
    assert ex.calls == []


# ---------- потолок по числу сессий ----------

async def test_session_cap_stops_paid_lanes(db, monkeypatch):
    """Третий потолок, и единственный, который не врёт при cost_usd=0:
    у задачи 531 в живой базе 24 строки Run при attempts=0, и ни один
    денежный счётчик этого не заметил."""
    monkeypatch.setattr(cfg, "daily_cc_sessions", 3)
    p = await make_project()
    ids = await make_tasks(p.id, 2, lane="build")
    async with Session() as s:
        for _ in range(3):
            s.add(Run(task_id=ids[0], kind="execute", ok=True, backend="cli",
                      cost_usd=0.0))
        await s.commit()

    ex = FakeExecutor()
    sched = make_sched(executor=ex)
    await drive(sched, ticks=3)
    assert sched.budget_paused is True
    assert ex.calls == []


# ---------- слот полосы ----------

async def test_cancelled_worker_releases_its_lane(db):
    """`await` в `finally` при отмене поднимает CancelledError, а он НЕ
    подкласс Exception: `except Exception` его не ловил, и `running[lane] -= 1`
    не выполнялся — полоса оставалась занятой навсегда, до перезапуска."""

    class Slow:
        async def run(self, task, project):
            await asyncio.sleep(30)
            return {}

    p = await make_project()
    await make_tasks(p.id, 1, lane="build")
    sched = make_sched(executor=Slow())
    await sched.tick()
    assert sched.running["build"] == 1

    for worker in list(sched._workers):
        worker.cancel()
    await asyncio.gather(*sched._workers, return_exceptions=True)
    await sched.drain()

    assert sched.running["build"] == 0, "полоса осталась занятой навсегда"
    assert not sched.inflight
    # Соединения aiosqlite живут в отдельных ПОТОКАХ и отдают результат
    # через `call_soon_threadsafe` в цикл, который их открыл. Отменённый
    # воркер обрывает запрос на полпути, а pytest-asyncio даёт каждому тесту
    # свой цикл: не дождавшись потока, получаем чужое «Event loop is closed»
    # в СЛЕДУЮЩЕМ тесте — и искать его будут там. Даём потоку доработать
    # и закрываем пул в том же цикле, в котором он открылся.
    await asyncio.sleep(0.05)
    await engine.dispose()


# ---------- политика ----------

async def test_policy_violation_stops_the_work(db, monkeypatch, tmp_path):
    """Секрет, записанный агентом в файл, останавливает ВСЮ работу — файлом
    PAUSED, а не паузой по квоте: та снимается сама через пять минут
    и объясняется на дашборде словами про подписку."""
    pause = tmp_path / "PAUSED"
    monkeypatch.setattr("autopilot.progress.pause_path", lambda: pause)

    p = await make_project()
    [task_id] = await make_tasks(p.id, 1, lane="build")
    comm = FakeCommunicator()
    sched = make_sched(communicator=comm)

    dirty = worktrace.Changed(created=(".env",), ok=True)
    monkeypatch.setattr(sched, "_trace_for", lambda project: None)

    async def fake_review(task, project, changed):
        return await Scheduler._policy_review(sched, task, project, dirty)

    monkeypatch.setattr(sched, "_policy_review", fake_review)
    await drive(sched, ticks=2)

    async with Session() as s:
        row = await s.get(Task, task_id)
    assert row.status == NEEDS_HUMAN
    assert "политики" in row.last_error
    assert pause.exists(), "инцидент не остановил работу"
    assert any("⛔" in n for n in comm.owner_notes)
