"""Не сломала ли новая задача то, что уже было принято.

Опасение верное: каждая задача проверяется своими критериями и ничем больше,
поэтому сломать соседнюю она может незаметно. Проверка ловит это ровно тем же
детерминированным кодом, что и обычная приёмка, — просто запущенным дважды.
"""
from __future__ import annotations

import pytest

from autopilot import paths, regression
from autopilot.config import cfg
from autopilot.db import Project, Session, Task
from conftest import make_project


def task(tid: int, checks: list[dict], status: str = "done", pid: int = 1) -> Task:
    return Task(id=tid, project_id=pid, title=f"t{tid}", status=status,
                acceptance=checks, lane="build")


# ---------- пути ----------

def test_paths_come_from_criteria():
    t = task(1, [{"type": "file_exists", "path": "backend/app/routers/chat.py"},
                 {"type": "shell", "cmd": "cd backend && pytest tests/test_cart.py"}])
    assert paths.of(t) == {"backend/app", "tests/test_cart.py"}


def test_tasks_in_different_areas_do_not_overlap():
    back = task(1, [{"type": "file_exists", "path": "backend/app/main.py"}])
    front = task(2, [{"type": "file_exists", "path": "client_app/lib/main.dart"}])
    assert paths.overlap(back, front) is False


def test_tasks_in_the_same_area_overlap():
    goods = task(1, [{"type": "file_exists", "path": "backend/app/routers/goods.py"}])
    orders = task(2, [{"type": "file_exists", "path": "backend/app/routers/orders.py"}])
    assert paths.overlap(goods, orders) is True


def test_unknown_paths_are_not_an_overlap():
    """Пустой список путей — не пересечение: про такую задачу мы просто
    ничего не знаем, и запрещать её на этом основании нельзя."""
    silent = task(1, [{"type": "http", "url": "http://localhost:8000/"}])
    other = task(2, [{"type": "file_exists", "path": "backend/app/main.py"}])
    assert paths.overlap(silent, other) is False


# ---------- отбор проверок ----------

class FakeVerifier:
    """Отдаёт заранее заданный результат по команде критерия."""

    def __init__(self, results: dict[str, bool]):
        self.results = results
        self.ran: list[str] = []

    @property
    def handlers(self):
        async def run(check, cwd, task, project):
            key = str(check.get("cmd") or check.get("path") or check.get("url"))
            self.ran.append(key)
            return self.results.get(key, True), ""
        return {"shell": run, "file_exists": run, "http": run, "dom": run}


async def seed(project_id: int, rows: list[Task]) -> None:
    async with Session() as s:
        for r in rows:
            s.add(Task(project_id=project_id, title=r.title, status=r.status,
                       acceptance=r.acceptance, lane="build"))
        await s.commit()


async def test_only_overlapping_done_tasks_are_rechecked(db):
    """Гонять все проверки проекта после каждой задачи — минуты впустую.
    Берём только те принятые задачи, что трогают те же каталоги."""
    p = await make_project()
    await seed(p.id, [
        task(0, [{"type": "file_exists", "path": "backend/app/models.py"}]),
        task(0, [{"type": "file_exists", "path": "client_app/lib/main.dart"}]),
    ])
    mine = task(99, [{"type": "file_exists", "path": "backend/app/routers/x.py"}],
                pid=p.id)

    pairs = await regression.at_risk(mine, p.id)
    assert [c["path"] for _, c in pairs] == ["backend/app/models.py"]


async def test_suspicious_checks_are_not_used_as_regression(db):
    """`http 200` на корне проходит и на пустом хостинге — его падение или
    прохождение ничего не доказывает, в том числе и здесь."""
    p = await make_project()
    await seed(p.id, [task(0, [
        {"type": "http", "url": "http://localhost:8000/", "expect": 200},
        {"type": "file_exists", "path": "backend/app/models.py"},
    ])])
    mine = task(99, [{"type": "file_exists", "path": "backend/app/routers/x.py"}],
                pid=p.id)
    pairs = await regression.at_risk(mine, p.id)
    kinds = [c.get("type") for _, c in pairs]
    assert kinds == ["file_exists"]


# ---------- база сравнения ----------

async def test_already_red_check_does_not_blame_this_task(db):
    """Главное правило: сломанным считается только то, что было зелёным ДО.

    Иначе первый же неподнятый сервер объявил бы виновной любую задачу.
    """
    p = await make_project()
    await seed(p.id, [task(0, [
        {"type": "shell", "cmd": "backend/app/broken.sh"}])])
    mine = task(99, [{"type": "file_exists", "path": "backend/app/x.py"}], pid=p.id)

    v = FakeVerifier({"backend/app/broken.sh": False})   # красный и до, и после
    async with Session() as s:
        project = await s.get(Project, p.id)

    before = await regression.snapshot(v, mine, project)
    assert before == {k: False for k in before}
    assert await regression.broken(v, mine, project, before) == []


async def test_breaking_a_green_check_is_caught(db):
    p = await make_project()
    await seed(p.id, [task(0, [
        {"type": "shell", "cmd": "backend/app/suite.sh"}])])
    mine = task(99, [{"type": "file_exists", "path": "backend/app/x.py"}], pid=p.id)
    async with Session() as s:
        project = await s.get(Project, p.id)

    green = FakeVerifier({})                       # всё проходит
    before = await regression.snapshot(green, mine, project)
    assert any(before.values()), "база должна быть зелёной"

    red = FakeVerifier({"backend/app/suite.sh": False})   # задача всё сломала
    broke = await regression.broken(red, mine, project, before)
    assert len(broke) == 1
    assert "сломана уже принятая задача" in broke[0]


async def test_switched_off_costs_nothing(db, monkeypatch):
    monkeypatch.setattr(cfg, "regression_check", False)
    p = await make_project()
    await seed(p.id, [task(0, [{"type": "shell", "cmd": "backend/app/suite.sh"}])])
    mine = task(99, [{"type": "file_exists", "path": "backend/app/x.py"}], pid=p.id)
    async with Session() as s:
        project = await s.get(Project, p.id)

    v = FakeVerifier({})
    assert await regression.snapshot(v, mine, project) == {}
    assert v.ran == []
