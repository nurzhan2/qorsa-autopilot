"""ВРЕМЕННАЯ проверка находки — удалить после прогона."""
from __future__ import annotations

import time

from autopilot import evidence, worktrace
from autopilot.db import Project, Session, log_attempt
from autopilot.verifier import Verifier
from conftest import make_project
from test_verifier import make_task


def test_a_snapshot_mechanics(tmp_path):
    """Сборка 1 создаёт тест, сборка 2 его переписывает."""
    ws = tmp_path
    (ws / "backend" / "tests").mkdir(parents=True)

    with worktrace.around(ws) as t1:
        (ws / "backend" / "tests" / "test_cart.py").write_text("def test_x(): assert 1", encoding="utf-8")
    print("сборка 1: created=", t1.changed.created, "touched=", t1.changed.touched)

    time.sleep(0.02)
    with worktrace.around(ws) as t2:
        (ws / "backend" / "tests" / "test_cart.py").write_text("def test_x(): assert 2  # правка", encoding="utf-8")
    print("сборка 2: created=", t2.changed.created, "touched=", t2.changed.touched)

    накоплено = set(t1.changed.touched) | set(t2.changed.touched)
    check = {"type": "shell", "cmd": "cd backend && pytest tests/test_cart.py"}
    print("review:", evidence.review([check], накоплено))
    assert t1.changed.touched == ()
    assert t2.changed.touched == ("backend/tests/test_cart.py",)
    assert evidence.review([check], накоплено) == {0: {"tests/test_cart.py"}}


async def test_b_end_to_end(db, tmp_path):
    """Конец в конец через настоящий Verifier."""
    p = await make_project()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cart.py").write_text("def test_x(): assert 1", encoding="utf-8")
    async with Session() as s:
        pr = await s.get(Project, p.id)
        pr.workspace = str(tmp_path)
        await s.commit()
        p = await s.get(Project, p.id)

    task = await make_task(p.id, [{"type": "file_exists", "path": "tests/test_cart.py"}])

    # сборка 1: файл СОЗДАН, пятна нет
    await log_attempt(task.id, project_id=p.id, lane="build", outcome="built",
                      counted=False, changed=["tests/test_cart.py"], touched=[])
    v1 = await Verifier().run(task, p)
    print("после сборки 1:", v1.ok, "det=", v1.deterministic, "confirmed=", v1.confirmed,
          "unproven=", v1.unproven, "tainted=", v1.tainted)

    # сборка 2 (после провала по ДРУГОМУ критерию) правит тот же файл
    await log_attempt(task.id, project_id=p.id, lane="build", outcome="built",
                      counted=False, changed=["tests/test_cart.py"],
                      touched=["tests/test_cart.py"])
    v2 = await Verifier().run(task, p)
    print("после сборки 2:", v2.ok, "det=", v2.deterministic, "confirmed=", v2.confirmed,
          "unproven=", v2.unproven, "tainted=", v2.tainted)

    assert v1.confirmed is True
    assert v2.confirmed is False and v2.unproven is True
