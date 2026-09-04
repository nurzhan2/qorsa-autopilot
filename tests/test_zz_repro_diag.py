"""ВРЕМЕННЫЙ тест-воспроизведение находки: бесплатная приёмка зовёт разбор."""
from __future__ import annotations

import time

import pytest
from conftest import FakeCommunicator, make_project

from autopilot import crew, limits
from autopilot.config import cfg
from autopilot.db import Session, Task
from autopilot.fakes import FakeExecutor, FakeVerifier
from autopilot.scheduler import Scheduler

FREE = [{"type": "shell", "cmd": "echo ok && exit 1"}]


async def _mk_task(project_id: int, attempts: int) -> int:
    async with Session() as s:
        t = Task(project_id=project_id, order_idx=0, lane="verify",
                 title="бесплатная приёмка", status="ready",
                 verify_class="auto", executor="claude_code",
                 acceptance=list(FREE), attempts=attempts, depends_on=[])
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()
    return tid


@pytest.mark.parametrize("mode", ["limit", "budget"])
async def test_free_verify_still_calls_the_model_on_failure(db, monkeypatch, mode):
    p = await make_project()
    tid = await _mk_task(p.id, attempts=2)

    monkeypatch.setattr(cfg, "crew_diagnose", True)
    called: list[int] = []

    async def fake_diagnose(self, task, project, defects):
        called.append(task.id)
        return None

    monkeypatch.setattr(crew.Diagnostician, "diagnose", fake_diagnose)

    if mode == "limit":
        limits.state.blocked_until = time.monotonic() + 3600
    else:
        monkeypatch.setattr(cfg, "daily_budget_usd", -1.0)   # потолок заведомо взят

    ver = FakeVerifier(ok=False, defects=["GET /x -> 500, ожидалось 200"])
    sched = Scheduler(FakeExecutor(), ver, FakeCommunicator())
    try:
        await sched.tick()
        await sched.drain()
    finally:
        limits.state.clear()

    async with Session() as s:
        t = await s.get(Task, tid)

    print(f"[{mode}] verifier.calls={ver.calls} diagnose_called={called} "
          f"attempts={t.attempts} status={t.status} lane={t.lane} "
          f"budget_paused={sched.budget_paused}")
    assert ver.calls == [tid], "бесплатная приёмка не поехала — воспроизвести нечего"
    assert called == [], f"разбор ушёл к модели при закрытом окне/бюджете: {called}"
