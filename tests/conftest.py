"""Общая обвязка тестов.

DB_URL подменяется ДО импорта autopilot: engine создаётся на импорте модуля db,
позже его уже не перенаправить. Настоящий Claude Code и Google API не дёргаются
нигде — вместо них autopilot.fakes и локальные заглушки.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qorsa-tests-"))
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["WORKSPACES"] = str(_TMP / "workspaces")
os.environ["LOGS"] = str(_TMP / "logs")
os.environ["ANTHROPIC_API_KEY"] = ""       # судья в тестах не поднимается
os.environ["SHEET_ID"] = ""

from autopilot.db import Base, Project, Session, Task, engine  # noqa: E402


@pytest.fixture
async def db():
    """Чистая база на каждый тест.

    dispose() обязателен: pytest-asyncio даёт каждому тесту свой event loop,
    а соединения из пула привязаны к тому loop, в котором открылись.
    """
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


async def make_project(status: str = "active", priority: int = 2,
                       deadline: dt.date | None = None, title: str = "проект",
                       tg_chat_id: str | None = "chat-1") -> Project:
    async with Session() as s:
        p = Project(client="клиент", title=title, status=status, priority=priority,
                    deadline=deadline, tg_chat_id=tg_chat_id)
        s.add(p)
        await s.commit()
    return p


async def make_tasks(project_id: int, n: int, lane: str = "build",
                     status: str = "ready", start_idx: int = 0) -> list[int]:
    ids = []
    async with Session() as s:
        for i in range(n):
            t = Task(project_id=project_id, order_idx=start_idx + i, lane=lane,
                     title=f"{lane} {i + 1}", status=status)
            s.add(t)
            await s.flush()
            ids.append(t.id)
        await s.commit()
    return ids


class FakeCommunicator:
    """Считает вызовы полосы chat, ничего никуда не шлёт."""

    def __init__(self):
        self.processed: list[int] = []
        self.done: list[int] = []

    async def process(self, task, project) -> None:
        self.processed.append(project.id)

    async def on_task_done(self, task, project) -> None:
        self.done.append(project.id)
