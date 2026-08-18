"""Окружение: критерий обязан быть самодостаточным, а чего нет — задача мне.

Первый живой прогон показал обе дыры разом: задача была сделана и провалена
приёмкой из-за чужого окружения, а половина плана в принципе непроверяема,
потому что нужных инструментов на машине нет.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import make_project

from autopilot import toolchain
from autopilot.db import Session, Task
from autopilot.verifier import Verifier, project_venv, shell_env


# ---------- верификатор находит окружение, созданное агентом ----------

async def test_pytest_runs_in_the_project_venv(db, tmp_path):
    """Задача с pytest в .venv проходит приёмку.

    Живой случай: агент сделал «API корзины», поднял окружение, прогнал
    pytest и получил 17 passed. Верификатор выполнил ту же команду голой,
    попал в СВОЁ окружение и провалил работу на ошибке импорта.
    """
    project_dir = tmp_path / "app"
    project_dir.mkdir()
    subprocess.run([sys.executable, "-m", "venv", str(project_dir / ".venv")],
                   check=True, capture_output=True)

    # модуль есть ТОЛЬКО в окружении проекта: снаружи импорт упадёт
    venv = project_venv(str(project_dir))
    assert venv is not None, "окружение проекта не найдено"
    site = list((project_dir / ".venv").glob("**/site-packages"))[0]
    (site / "cart_lib.py").write_text("VALUE = 17\n", encoding="utf-8")
    (project_dir / "check.py").write_text(
        "import cart_lib; assert cart_lib.VALUE == 17; print('17 passed')\n",
        encoding="utf-8")

    p = await make_project()
    async with Session() as s:
        row = await s.get(type(p), p.id)
        row.workspace = str(project_dir)
        await s.commit()
        p = await s.get(type(p), p.id)

    async with Session() as s:
        task = Task(project_id=p.id, lane="verify", status="ready",
                    title="API корзины", verify_class="auto",
                    executor="claude_code", depends_on=[],
                    acceptance=[{"type": "shell", "cmd": "python check.py"}])
        s.add(task)
        await s.commit()
        task = await s.get(Task, task.id)

    verdict = await Verifier().run(task, p)
    assert verdict.ok is True, f"приёмка не нашла окружение проекта: {verdict.defects}"
    assert verdict.confirmed is True


def test_shell_env_is_none_without_a_venv(tmp_path):
    """Нет окружения — нет и подмены: ничего не выдумываем."""
    assert project_venv(str(tmp_path)) is None
    assert shell_env(str(tmp_path)) is None


# ---------- чего не хватает на машине ----------

def test_commands_are_extracted_from_criteria():
    assert "pytest" in toolchain.commands_in("cd backend && pytest tests/ -v")
    assert "flutter" in toolchain.commands_in("cd app && flutter build apk --debug")
    # критерий, назвавший окружение явно, инструментом системы не считается
    assert toolchain.commands_in("cd backend && .venv/Scripts/python -m pytest") == set()
    # cd сама по себе ничего не требует
    assert "cd" not in toolchain.commands_in("cd backend && alembic upgrade head")


def test_missing_tools_ignores_project_level_ones(monkeypatch):
    """pytest и alembic ставит сам агент в .venv — это не дыра в окружении."""
    monkeypatch.setattr(toolchain, "installed", lambda t: False)
    tasks = [{"title": "тесты", "acceptance": [{"type": "shell", "cmd": "pytest tests/"}]}]
    assert toolchain.missing_tools(tasks) == []

    tasks = [{"title": "сборка", "acceptance": [{"type": "shell", "cmd": "flutter build apk"}]}]
    missing = toolchain.missing_tools(tasks)
    assert [m["tool"] for m in missing] == ["flutter"]
    assert "docs.flutter.dev" in missing[0]["how"]


def test_stack_names_what_criteria_forget(monkeypatch):
    """`alembic upgrade head` не упоминает PostgreSQL, а без базы бесполезен."""
    monkeypatch.setattr(toolchain, "installed", lambda t: False)
    brief = {"stack": [{"text": "Бэкенд: FastAPI + PostgreSQL, монолит"}]}
    missing = toolchain.missing_tools([], brief)
    assert [m["tool"] for m in missing] == ["psql"]


def test_setup_tasks_are_manual_and_first():
    """Ставить SDK агент не должен: он уже раздул каталог до 3.7 ГБ."""
    missing = [{"tool": "flutter", "name": "Flutter SDK", "how": "docs",
                "tasks": ["Flutter клиент: каталог"]}]
    tasks = toolchain.setup_tasks(missing)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["executor"] == "manual", "установку SDK отдали агенту"
    assert t["setup"] is True
    assert t["acceptance"] == [{"type": "shell", "cmd": "flutter --version"}]
    assert "Flutter SDK" in t["title"]


def test_executor_prompt_forbids_downloading_sdk():
    """Запрет качать тулчейн внутрь рабочего каталога — в промпте агента."""
    from autopilot.executor import SYSTEM_RULES

    assert "НЕ КАЧАЙ SDK" in SYSTEM_RULES
    assert "3.7 ГБ" in SYSTEM_RULES, "нет объяснения, чем это кончилось"


def test_planner_prompt_requires_explicit_interpreter():
    from autopilot.planner import SYSTEM

    assert ".venv/Scripts/python -m pytest" in SYSTEM
    assert "САМОДОСТАТОЧНЫМ" in SYSTEM
