"""Оборванный прогон убирает за собой, и один проект пишет один процесс.

Оба случая уже происходили на живых данных:

* `brief_eval`, убитый на десятой минуте, оставил бриф проекта 8 обнулённым:
  восстановление висело на `except`, а до него дело не дошло;
* два `plan_eval` на проекте 8 писали задачи одновременно и перемешали план.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

from autopilot import guard


# ---------- замок на проект ----------

def test_second_run_on_same_project_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path)
    with guard.project_lock(7, "план"):
        with pytest.raises(guard.BusyProject) as exc:
            with guard.project_lock(7, "ещё один план"):
                pass
    # в сообщении должно быть видно, кто держит и почему второму нельзя
    assert "7" in str(exc.value)


def test_lock_is_released_after_use(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path)
    with guard.project_lock(7, "план"):
        pass
    with guard.project_lock(7, "второй заход после первого"):
        pass          # не должно бросить: замок отпущен


def test_lock_released_even_after_failure(tmp_path, monkeypatch):
    """Упавший прогон не должен запирать проект навсегда."""
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        with guard.project_lock(7, "план"):
            raise RuntimeError("прогон упал")
    with guard.project_lock(7, "следующий"):
        pass


def test_different_projects_do_not_block_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path)
    with guard.project_lock(7, "план"), guard.project_lock(8, "бриф"):
        pass


def test_lock_holds_across_processes(tmp_path, monkeypatch):
    """Главное свойство: замок видит ДРУГОЙ процесс, а не только этот.

    Внутрипроцессная проверка сама по себе бесполезна — два `plan_eval`
    запускались именно из разных терминалов.
    """
    monkeypatch.setattr(guard, "LOCK_DIR", tmp_path)
    root = str(guard.cfg.root)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from autopilot import guard\n"
        "from pathlib import Path\n"
        "guard.LOCK_DIR = Path(%r)\n"
        "try:\n"
        "    with guard.project_lock(7, 'чужой процесс'):\n"
        "        print('GOT')\n"
        "except guard.BusyProject:\n"
        "    print('BUSY')\n" % (root, str(tmp_path))
    )
    with guard.project_lock(7, "наш процесс"):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=120)
        assert "BUSY" in out.stdout, f"чужой процесс вошёл в занятый проект: {out}"

    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert "GOT" in out.stdout, f"замок не отпущен для чужого процесса: {out}"


# ---------- дочерние процессы ----------

class FakeProc:
    def __init__(self, alive=True, pid=None):
        self.returncode = None if alive else 0
        self.pid = pid
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_kill_children_kills_only_living():
    alive, dead = FakeProc(), FakeProc(alive=False)
    guard.watch_child(alive)
    guard.watch_child(dead)
    assert guard.kill_children() == 1
    assert alive.killed is True and dead.killed is False
    # реестр опустел: второй вызов никого не найдёт
    assert guard.kill_children() == 0


def test_forget_child_removes_from_registry():
    proc = FakeProc()
    guard.watch_child(proc)
    guard.forget_child(proc)
    assert guard.kill_children() == 0
    assert proc.killed is False


async def test_cli_kills_child_when_progon_cancelled(monkeypatch):
    """Отмена прогона не должна оставлять claude работать в одиночку.

    Осиротевший процесс дожигает то же пятичасовое окно, что и работа
    владельца руками, и заметить это можно только по опустевшей квоте.
    """
    from autopilot.llm import CliBackend

    started = {}

    class Proc:
        returncode = None
        pid = None

        def __init__(self):
            self.killed = False

        async def communicate(self, input=None):
            await asyncio.sleep(30)          # «модель думает»
            return b"{}", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

    async def fake_exec(*args, **kwargs):
        started["proc"] = Proc()
        return started["proc"]

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    task = asyncio.create_task(CliBackend().ask("вопрос"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert started["proc"].killed is True, "дочерний claude пережил прогон"


def test_recovery_can_still_await_after_the_signal():
    """Восстановление ходит в базу — значит после сигнала await обязан работать.

    Тонкость асинхронной отмены: если бы `await` в блоке `except` снова
    получал CancelledError, восстановление брифа обрывалось бы на первом же
    запросе, и мы вернулись бы ровно к тому, что чиним.
    """
    done = []

    async def restore():
        await asyncio.sleep(0)          # изображаем запись в базу
        done.append("бриф возвращён")

    async def work():
        try:
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(5)
        except BaseException:
            await restore()
            raise

    with pytest.raises(guard.Interrupted):
        guard.run(work())
    assert done == ["бриф возвращён"]


def test_guard_run_returns_result():
    async def work():
        await asyncio.sleep(0)
        return 42

    assert guard.run(work()) == 42


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_becomes_an_exception_inside_the_coroutine(signum):
    """Ради чего всё: обработчик восстановления обязан отработать.

    Сигнал приходит В корутину как отмена, а не убивает процесс мимо
    питоновского кода. Раньше убитый прогон не доходил до `except` вовсе —
    бриф оставался обнулённым, хотя код восстановления был написан.

    Оговорка про Windows: перехватывается то, что доставлено через
    механизм сигналов. `taskkill /F` не перехватить ничем — от него
    спасает только job object, убивающий детей вместе с нами.
    """
    marks = []
    child = FakeProc()
    guard.watch_child(child)

    async def work():
        try:
            signal.raise_signal(signum)
            await asyncio.sleep(5)
        except BaseException:
            marks.append("восстановил")
            raise

    with pytest.raises(guard.Interrupted):
        guard.run(work())
    assert marks == ["восстановил"], "восстановление не отработало"
    assert child.killed is True, "дочерний процесс пережил сигнал"


@pytest.mark.skipif(os.name == "nt",
                    reason="os.kill(SIGTERM) на Windows — это TerminateProcess, "
                           "его не перехватить ничем")
def test_sigterm_from_outside_is_caught():
    """Ради чего всё: обработчик восстановления обязан отработать.

    Убитый прогон раньше не доходил до `except` вовсе — бриф оставался
    обнулённым, хотя код восстановления был написан и работал на исключениях.
    """
    marks = []

    async def work():
        try:
            os.kill(os.getpid(), 15)
            await asyncio.sleep(5)
        except BaseException:
            marks.append("восстановил")
            raise

    with pytest.raises(guard.Interrupted):
        guard.run(work())
    assert marks == ["восстановил"]
