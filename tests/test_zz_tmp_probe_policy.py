"""ВРЕМЕННЫЙ пробник (удалить после проверки находки)."""
from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from conftest import FakeCommunicator, make_project, make_tasks
from sqlalchemy import select

from autopilot import policy, progress, projectdir, worktrace
from autopilot.db import Session, Task
from autopilot.fakes import FakeVerifier
from autopilot.manual import NEEDS_HUMAN
from autopilot.scheduler import Scheduler
from autopilot.vault import Vault

SECRET = "postgresql+asyncpg://qorsa:pa55word-long@localhost:5432/delivery"


class AppendingExecutor:
    """Агент, который делает работу и дописывает строку в DECISIONS.md —
    ровно то, что велит ему SYSTEM_RULES."""

    def __init__(self, ws):
        self.ws = ws
        self.calls = 0

    async def run(self, task, project):
        self.calls += 1
        (self.ws / "backend").mkdir(parents=True, exist_ok=True)
        (self.ws / "backend" / f"m{self.calls}.py").write_text(
            "import os\nDB = os.environ['DATABASE_URL']\n", encoding="utf-8")
        d = self.ws / "docs" / "DECISIONS.md"
        d.write_text(d.read_text(encoding="utf-8") +
                     f"\n## Решение {self.calls}\nвзял FastAPI\n", encoding="utf-8")
        return {"ok": True}


async def test_pre_existing_leak_halts_on_every_touch(db, monkeypatch, tmp_path):
    pause = tmp_path / "PAUSED"
    monkeypatch.setattr("autopilot.progress.pause_path", lambda: pause)

    store = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    store.set("DATABASE_URL", SECRET)
    monkeypatch.setattr(policy, "default_vault", store)

    p = await make_project()
    ws = projectdir.workspace(p)
    (ws / "docs").mkdir(parents=True, exist_ok=True)
    # Утечка УЖЕ в файле — как в живом каталоге проекта 8
    (ws / "docs" / "DECISIONS.md").write_text(
        f"# Решения\nПеременная окружения: `{SECRET}`\n", encoding="utf-8")

    ids = await make_tasks(p.id, 2, lane="build")
    comm = FakeCommunicator()
    ex = AppendingExecutor(ws)
    sched = Scheduler(ex, FakeVerifier(), comm)

    await sched.tick()
    await asyncio.sleep(0.2)
    await sched.drain()

    async with Session() as s:
        rows = (await s.execute(select(Task).where(Task.project_id == p.id))
                ).scalars().all()
    print("СЕССИЙ АГЕНТА:", ex.calls)
    print("СТАТУСЫ:", [(t.id, t.status, t.last_error) for t in rows])
    print("PAUSED существует:", pause.exists())
    if pause.exists():
        print("СОДЕРЖИМОЕ PAUSED:", pause.read_text(encoding="utf-8"))
    print("УВЕДОМЛЕНИЯ:", comm.owner_notes)

    assert pause.exists()
    assert any(t.status == NEEDS_HUMAN for t in rows)

    # Снимаем паузу рукой и даём агенту второй заход — повторится ли
    pause.unlink()
    async with Session() as s:
        for t in (await s.execute(select(Task).where(Task.project_id == p.id))
                  ).scalars().all():
            t.status, t.lane = "ready", "build"
        await s.commit()
    await sched.tick()
    await asyncio.sleep(0.2)
    await sched.drain()
    print("ПОСЛЕ СНЯТИЯ ПАУЗЫ, PAUSED снова:", pause.exists())
    assert pause.exists(), "второй заход не повторил остановку"
