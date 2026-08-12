"""Заглушки вместо Claude Code и судьи.

Используются в DRY_RUN=1 и в тестах: планировщик крутится вживую, но ни
процессы, ни платные API не дёргаются. Никакой отдельной логики здесь нет —
только та же сигнатура, что у Executor.run / Verifier.run.
"""
from __future__ import annotations

import asyncio
import logging
import random

from .db import Project, Task

log = logging.getLogger("fake")


class FakeExecutor:
    """Изображает сборку: спит и запоминает, кого обслужили."""

    def __init__(self, delay: float | tuple[float, float] = 0.0, fail_for: set[int] | None = None):
        self.delay = delay
        self.fail_for = fail_for or set()
        self.served: list[int] = []          # project_id в порядке обслуживания
        self.calls: list[tuple[int, int]] = []   # (project_id, task_id)

    def _sleep_for(self) -> float:
        if isinstance(self.delay, tuple):
            return random.uniform(*self.delay)
        return self.delay

    async def run(self, task: Task, project: Project) -> dict:
        self.served.append(project.id)
        self.calls.append((project.id, task.id))
        log.info("сборка: проект %s (%s) / задача %s", project.id, project.title, task.title)
        d = self._sleep_for()
        if d:
            await asyncio.sleep(d)
        if task.id in self.fail_for:
            raise RuntimeError("fake build failure")
        return {"session_id": f"fake-{task.id}", "total_cost_usd": 0.0}


class FakeVerifier:
    """Приёмка без судьи: по умолчанию всё проходит."""

    def __init__(self, ok: bool = True, defects: list[str] | None = None, delay: float = 0.0):
        self.ok = ok
        self.defects = defects or ["fake defect"]
        self.delay = delay
        self.calls: list[int] = []

    async def run(self, task: Task, project: Project) -> tuple[bool, list[str]]:
        self.calls.append(task.id)
        log.info("приёмка: проект %s / задача %s", project.id, task.title)
        if self.delay:
            await asyncio.sleep(self.delay)
        return (True, []) if self.ok else (False, list(self.defects))
