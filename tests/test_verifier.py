"""Фаза 5: приёмка без исполнимых проверок невозможна по конструкции."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeCommunicator, make_project
from sqlalchemy import select

from autopilot import checks, manual
from autopilot.db import Project, Session, Task
from autopilot.fakes import FakeExecutor
from autopilot.manual import NEEDS_HUMAN
from autopilot.planner import autonomy, classify
from autopilot.scheduler import Scheduler
from autopilot.verifier import Verifier

# страница с блоком, который дорисовывает скрипт: по сырому HTML его не видно
JS_PAGE = (
    "data:text/html,<html><head><title>Тест</title></head><body>"
    "<div id='static'>статика</div>"
    "<script>document.body.insertAdjacentHTML('beforeend',"
    "\"<div class='js-made'>отрисовано скриптом</div>\")</script>"
    "</body></html>"
)


async def make_task(project_id: int, acceptance: list, **kw) -> Task:
    async with Session() as s:
        t = Task(project_id=project_id, lane=kw.pop("lane", "verify"), status="ready",
                 title=kw.pop("title", "задача"), acceptance=acceptance,
                 verify_class=kw.pop("verify_class", "auto"),
                 executor=kw.pop("executor", "claude_code"),
                 depends_on=[], **kw)
        s.add(t)
        await s.commit()
        return await s.get(Task, t.id)


# ---------- контракт типов ----------

def test_check_registry_matches():
    """Множества типов у планировщика и верификатора обязаны совпадать.

    Разрыв между ними однажды дал приёмку вообще без проверок: planner
    выдавал dom, verifier его не знал и молча пропускал.
    """
    from autopilot import planner

    verifier_kinds = set(Verifier().handlers)
    registry_kinds = set(checks.REGISTRY)
    planner_kinds = set(planner.KNOWN_CHECKS)

    assert verifier_kinds == registry_kinds, (
        f"верификатор не покрывает реестр: не умеет {registry_kinds - verifier_kinds}, "
        f"лишнее {verifier_kinds - registry_kinds}")
    assert planner_kinds == registry_kinds, (
        f"планировщик разошёлся с реестром: {planner_kinds ^ registry_kinds}")

    # и промпт планировщика собирается из того же реестра
    for kind in registry_kinds:
        assert kind in planner.SYSTEM, f"тип {kind} не описан в промпте планировщика"


def test_registry_splits_deterministic_and_assisted():
    assert set(checks.DETERMINISTIC) == {"shell", "http", "file_exists", "dom"}
    assert set(checks.ASSISTED) == {"llm", "screenshot"}
    assert set(checks.HUMAN_ONLY) == {"human"}


# ---------- нет проверки = провал ----------

async def test_no_checks_is_failure(db):
    """Задача без исполнимых проверок не может стать done."""
    p = await make_project()
    v = Verifier()

    empty = await make_task(p.id, [])
    ok, defects = await v.run(empty, p)
    assert ok is False
    assert any("нет ни одной исполнимой проверки" in d for d in defects)

    unknown = await make_task(p.id, [{"type": "playwright_magic", "cmd": "x"}])
    ok, defects = await v.run(unknown, p)
    assert ok is False
    assert any("неизвестный тип" in d for d in defects)

    # критерий без обязательного поля — тоже не проверка
    broken = await make_task(p.id, [{"type": "shell"}])
    ok, defects = await v.run(broken, p)
    assert ok is False
    assert any("не запустится" in d for d in defects)

    # human честно говорит, что машина не судья
    human = await make_task(p.id, [{"type": "human", "criteria": "нравится дизайн"}])
    ok, defects = await v.run(human, p)
    assert ok is False
    assert any("human:" in d for d in defects)


async def test_unverifiable_task_goes_to_needs_human(db):
    """Повторять сборку бессмысленно — задача сразу уходит человеку."""
    p = await make_project()
    async with Session() as s:
        t = Task(project_id=p.id, lane="verify", status="ready", title="без критериев",
                 acceptance=[], verify_class="auto", executor="claude_code", depends_on=[])
        s.add(t)
        await s.commit()
        task_id = t.id

    sched = Scheduler(FakeExecutor(), Verifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()

    async with Session() as s:
        row = await s.get(Task, task_id)
    assert row.status == NEEDS_HUMAN, "задача с непроверяемыми критериями ушла в повтор"
    assert row.attempts == 1, "повторять такое не надо ни разу"


# ---------- dom ----------

async def test_dom_check_js_rendered(db):
    """Селектор, отрисованный скриптом, находится — значит браузер настоящий."""
    p = await make_project()
    v = Verifier()

    ok, msg = await v._check_dom(
        {"type": "dom", "url": JS_PAGE, "selector": ".js-made"}, ".", None, p)
    assert ok is True, f"JS-отрисованный блок не найден: {msg}"

    ok, msg = await v._check_dom(
        {"type": "dom", "url": JS_PAGE, "selector": "#static"}, ".", None, p)
    assert ok is True

    ok, msg = await v._check_dom(
        {"type": "dom", "url": JS_PAGE, "selector": ".no-such-thing"}, ".", None, p)
    assert ok is False
    assert "найден 0 раз" in msg
    assert "Тест" in msg, "в сообщении должно быть видно, что мы реально открыли"


async def test_dom_min_count(db):
    p = await make_project()
    page = ("data:text/html,<html><body><i class='x'>1</i><i class='x'>2</i>"
            "</body></html>")
    v = Verifier()
    ok, _ = await v._check_dom({"url": page, "selector": ".x", "min_count": 2}, ".", None, p)
    assert ok is True
    ok, msg = await v._check_dom({"url": page, "selector": ".x", "min_count": 5}, ".", None, p)
    assert ok is False and "нужно 5" in msg


# ---------- file_exists ----------

async def test_file_exists_sandboxed(db, tmp_path):
    """Путь за пределы каталога проекта отвергается."""
    p = await make_project()
    (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    v = Verifier()
    root = str(tmp_path)

    ok, _ = await v._check_file_exists({"path": "style.css"}, root, None, p)
    assert ok is True

    ok, msg = await v._check_file_exists({"path": "нет-такого.css"}, root, None, p)
    assert ok is False and "нет" in msg

    for outside in ("../secrets.enc", "../../etc/passwd", "sub/../../outside.txt"):
        ok, msg = await v._check_file_exists({"path": outside}, root, None, p)
        assert ok is False, f"путь {outside} проскочил песочницу"
        assert "выходит за пределы" in msg


# ---------- screenshot ----------

def test_screenshot_class():
    """screenshot даёт assisted, а не auto: судит модель, а не код."""
    assert checks.REGISTRY["screenshot"].deterministic is False
    assert checks.REGISTRY["screenshot"].assisted is True
    shot = {"type": "screenshot", "url": "https://x/", "criteria": "не разъезжается"}
    assert classify({"acceptance": [shot]}, "auto") == "assisted"
    # и три ширины заданы явно
    assert [w for w, _ in checks.VIEWPORTS] == [375, 768, 1440]


# ---------- ручные задачи ----------

class OkVerifier:
    def __init__(self, ok=True, defects=None):
        self.ok = ok
        self.defects = defects or ["критерий не прошёл"]
        self.calls = 0

    async def run(self, task, project):
        self.calls += 1
        return (True, []) if self.ok else (False, list(self.defects))


async def test_manual_task_verified(db):
    """Отметка «сделал» прогоняется через тот же верификатор, а не верится на слово."""
    p = await make_project()
    task = await make_task(
        p.id, [{"type": "http", "url": "https://example.kz/"}],
        title="Установить плагин", executor="manual", verify_class="auto")

    # критерии не прошли — задача НЕ закрывается
    bad = OkVerifier(ok=False, defects=["http: 404 вместо 200"])
    ok, defects = await manual.submit(task.id, bad)
    assert ok is False and bad.calls == 1
    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == NEEDS_HUMAN
    assert row.defects == ["http: 404 вместо 200"]

    # поправили — принято
    good = OkVerifier(ok=True)
    ok, _ = await manual.submit(task.id, good)
    assert ok is True and good.calls == 1
    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == "done"


async def test_manual_without_checks_not_accepted(db):
    """Даже с моих слов задача без критериев не закрывается."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "human", "criteria": "нравится"}],
                           executor="manual", verify_class="human")
    v = OkVerifier(ok=True)
    ok, defects = await manual.submit(task.id, v)

    assert ok is False
    assert v.calls == 0, "верификатор незачем звать: исполнимых критериев нет"
    assert any("принять нечем" in d for d in defects)
    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == NEEDS_HUMAN


async def test_manual_report_lists_criteria(db):
    """Список для человека содержит критерии, иначе его нельзя выполнить."""
    p = await make_project(title="магазин")
    await make_task(p.id, [{"type": "dom", "url": "https://x/shop/", "selector": ".product"}],
                    title="Импорт товаров", executor="manual", lane="build")
    await make_task(p.id, [{"type": "shell", "cmd": "npm run build"}],
                    title="Сборка темы", executor="claude_code", lane="build")

    text = await manual.report(p.id)
    assert "Импорт товаров" in text
    assert ".product" in text, "критерий должен быть виден"
    assert "Сборка темы" not in text, "задачи агента в ручной список не идут"


# ---------- автономность по времени ----------

def test_autonomy_by_time():
    """Доля по времени считается отдельно от доли по числу задач."""
    tasks = [{"verify_class": "auto", "executor": "claude_code", "estimate_min": 600}]
    tasks += [{"verify_class": "human", "executor": "manual", "estimate_min": 20}
              for _ in range(9)]
    stats = autonomy(tasks)

    assert stats["auto_ratio"] == 0.1, "по числу задач автопилот закрывает десятую часть"
    assert stats["auto_ratio_time"] == 0.77, "а по времени — три четверти"
    assert stats["minutes"] == 780 and stats["minutes_auto"] == 600
    # порог берёт лучшую из двух: проект осмысленный, хотя мелких задач много
    assert stats["suitable"] is True

    # без оценок времени вторая цифра не выдумывается
    no_time = [{"verify_class": "auto", "executor": "claude_code", "estimate_min": 0}]
    assert autonomy(no_time)["auto_ratio_time"] == 0.0
