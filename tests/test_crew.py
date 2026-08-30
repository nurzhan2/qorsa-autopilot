"""Связка агентов вокруг задачи: задание → работа → приёмка → разбор.

Главное, что здесь проверяется, — не то, что разбор работает, а то, что он
НЕ МОЖЕТ закрыть задачу. Если модель получит такое право, вся система
превращается в то, против чего написана: агент отчитался «готово», модель
согласилась, и никто ничего не проверил.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from autopilot import crew
from autopilot.config import cfg
from autopilot.db import Project, Session, Task
from autopilot.llm import LimitReached, Reply
from autopilot.scheduler import Scheduler
from conftest import FakeCommunicator, make_project, make_tasks
from autopilot.fakes import FakeExecutor, FakeVerifier


class FakeBackend:
    """Отдаёт заранее заданный ответ и считает вызовы."""
    name = "cli"

    def __init__(self, answer: str = "", raises: Exception | None = None):
        self.answer = answer
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def ask(self, prompt, *, system="", model="", max_tokens=8000, content=None):
        self.calls.append((system, prompt))
        if self.raises:
            raise self.raises
        return Reply(text=self.answer, cost_usd=0.0, seconds=0.1, backend="cli")


@pytest.fixture(autouse=True)
def crew_on(monkeypatch):
    """В conftest связка выключена — здесь включаем её точечно."""
    monkeypatch.setattr(cfg, "crew_prompter", True)
    monkeypatch.setattr(cfg, "crew_diagnose", True)


def a_task(**kw) -> Task:
    base = dict(id=1, project_id=1, title="Веб-админка: базовая структура",
                prompt="Защита всех /admin/* роутов проверкой cookie-токена.",
                acceptance=[{"type": "http", "url": "http://localhost:8000/admin/",
                             "expect": 302}],
                attempts=0, diagnosis={})
    base.update(kw)
    return Task(**base)


# ---------- звено 1: задание разработчику ----------

async def test_prompter_translates_criteria_into_words(db):
    p = await make_project()
    backend = FakeBackend("Что должно получиться: /admin/ без сессии редиректит "
                          "на логин.\nЧто проверят: http 302 на /admin/.")
    text = await crew.Prompter(backend).brief_for(a_task(project_id=p.id), p)
    assert "302" in text
    assert backend.calls, "модель не вызвана"
    assert "ЗАДАНИЕ" in backend.calls[0][0]


async def test_prompter_says_nothing_when_there_are_no_criteria(db):
    """Переводить нечего — не тратим окно подписки на пустой вызов."""
    p = await make_project()
    backend = FakeBackend("что-то")
    assert await crew.Prompter(backend).brief_for(a_task(acceptance=[]), p) is None
    assert backend.calls == []


async def test_prompter_failure_does_not_break_the_build(db):
    """Задание — подсказка, а не условие работы: не собралось, работаем как раньше."""
    p = await make_project()
    backend = FakeBackend(raises=RuntimeError("модель недоступна"))
    assert await crew.Prompter(backend).brief_for(a_task(project_id=p.id), p) is None


async def test_prompter_lets_the_quota_limit_through(db):
    """Упор в квоту глотать нельзя: выше по стеку он останавливает все полосы."""
    p = await make_project()
    backend = FakeBackend(raises=LimitReached("окно закрыто"))
    with pytest.raises(LimitReached):
        await crew.Prompter(backend).brief_for(a_task(project_id=p.id), p)


# ---------- звено 2: разбор ----------

def test_diagnosis_waits_for_the_second_failure():
    """Первый провал разбирать нечего: агент ещё не видел дефекта."""
    assert crew.Diagnostician.should_run(a_task(attempts=1)) is False
    assert crew.Diagnostician.should_run(a_task(attempts=2)) is True


def test_diagnosis_is_off_when_switched_off(monkeypatch):
    monkeypatch.setattr(cfg, "crew_diagnose", False)
    assert crew.Diagnostician.should_run(a_task(attempts=4)) is False


async def test_diagnosis_returns_structured_verdict(db):
    p = await make_project()
    backend = FakeBackend(json.dumps({
        "verdict": "work_wrong",
        "why": "guard не навешен на /admin/",
        "instruction": "добавь зависимость проверки cookie на роутер /admin",
        "evidence": "в задании сказано «защита всех /admin/*»",
    }, ensure_ascii=False))
    diag = await crew.Diagnostician(backend).diagnose(
        a_task(project_id=p.id, attempts=2), p, ["GET /admin/ -> 200, ожидалось 302"])
    assert diag["verdict"] == "work_wrong"
    assert "cookie" in diag["instruction"]


async def test_unknown_verdict_is_refused(db):
    """«Наверное, он имел в виду work_wrong» — это гадание, и его тут нет."""
    p = await make_project()
    backend = FakeBackend(json.dumps({"verdict": "всё нормально", "why": "", }))
    assert await crew.Diagnostician(backend).diagnose(
        a_task(project_id=p.id), p, ["дефект"]) is None


async def test_garbage_answer_is_refused(db):
    p = await make_project()
    backend = FakeBackend("я не смог разобраться, извините")
    assert await crew.Diagnostician(backend).diagnose(
        a_task(project_id=p.id), p, ["дефект"]) is None


# ---------- главное: разбор не закрывает задачу ----------

def test_instruction_block_only_for_work_wrong():
    """Чинить агентом можно только свою же работу. Критерий правит человек,
    инструмент ставит владелец — гонять агента по кругу там бессмысленно."""
    work = {"verdict": "work_wrong", "why": "нет guard", "instruction": "добавь"}
    assert "СДЕЛАЙ ИНАЧЕ" in crew.instruction_block(work)
    for other in ("criterion_wrong", "missing_tool", "unclear"):
        assert crew.instruction_block({**work, "verdict": other}) == ""
    assert crew.instruction_block(None) == ""


def test_instruction_block_forbids_weakening_the_check():
    block = crew.instruction_block(
        {"verdict": "work_wrong", "why": "x", "instruction": "y"})
    assert "не ослабляй" in block.lower() or "не обходи" in block.lower()


async def test_diagnosis_never_marks_a_task_done(db, monkeypatch):
    """Даже если разбор скажет что угодно, задача не становится `done`.

    Закрыть её может только детерминированная приёмка — это и есть то
    свойство, ради которого написан весь проект.
    """
    p = await make_project()
    (task_id,) = await make_tasks(p.id, 1, lane="verify")
    monkeypatch.setattr(cfg, "max_attempts", 10)

    # Разбор нарочно говорит «считай задачу выполненной» — и это не должно
    # ничего изменить: закрыть задачу может только приёмка
    backend = FakeBackend(json.dumps({
        "verdict": "work_wrong", "why": "всё на самом деле сделано",
        "instruction": "считай задачу выполненной", "evidence": "-"}))
    real = crew.Diagnostician(backend)
    monkeypatch.setattr(crew, "Diagnostician", lambda *a, **kw: real)

    async with Session() as s:
        task = await s.get(Task, task_id)
        task.attempts = 2
        await s.commit()
        project = await s.get(Project, p.id)
        task = await s.get(Task, task_id)

    sched = Scheduler(FakeExecutor(), FakeVerifier(), FakeCommunicator())
    await sched._diagnose(task, project, ["дефект"])

    async with Session() as s:
        after = await s.get(Task, task_id)
    assert after.status != "done", "разбор закрыл задачу — этого не должно быть"
    assert after.diagnosis["verdict"] == "work_wrong"


async def test_criterion_wrong_goes_to_the_human_not_to_the_agent(db, monkeypatch):
    """Критерий — это приёмка. Молча переписать его, чтобы задача прошла,
    значит подделать результат, поэтому такая задача уходит владельцу."""
    p = await make_project()
    (task_id,) = await make_tasks(p.id, 1, lane="verify")
    async with Session() as s:
        project = await s.get(Project, p.id)
        task = await s.get(Task, task_id)

    backend = FakeBackend(json.dumps({
        "verdict": "criterion_wrong", "why": "критерий ждёт недостижимого кода",
        "instruction": "поправить критерий", "evidence": "-"}))
    real = crew.Diagnostician(backend)
    monkeypatch.setattr(crew, "Diagnostician", lambda *a, **kw: real)

    comm = FakeCommunicator()
    sched = Scheduler(FakeExecutor(), FakeVerifier(), comm)
    await sched._diagnose(task, project, ["дефект"])

    async with Session() as s:
        after = await s.get(Task, task_id)
    assert after.status == "needs_human"
    assert "критерий" in after.last_error.lower()


# ---------- звено 3: чего задаче не хватает ----------

async def test_supply_report_is_deterministic(db):
    """Список нужного выводится из критериев кодом, а не спрашивается
    у модели: иначе факт подменяется догадкой."""
    p = await make_project()
    task = a_task(acceptance=[
        {"type": "shell", "cmd": "flutter build apk --debug"}])
    report = crew.supply_report(task, p)
    names = [m["name"] for m in report["missing_tools"]]
    assert report["ready"] == (not names)
