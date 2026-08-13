"""Фаза 5: приёмка без исполнимых проверок невозможна по конструкции."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeCommunicator, make_project
from sqlalchemy import select

from autopilot import checks, manual
from autopilot.db import Project, Session, Task
from autopilot.fakes import FakeExecutor, FakeVerifier
from autopilot.manual import NEEDS_HUMAN
from autopilot.planner import autonomy, classify
from autopilot.scheduler import Scheduler
from autopilot.verifier import Verdict, Verifier

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
    verdict = await v.run(empty, p)
    ok, defects = verdict.ok, verdict.defects
    assert ok is False
    assert any("нет ни одной исполнимой проверки" in d for d in defects)

    unknown = await make_task(p.id, [{"type": "playwright_magic", "cmd": "x"}])
    verdict = await v.run(unknown, p)
    ok, defects = verdict.ok, verdict.defects
    assert ok is False
    assert any("неизвестный тип" in d for d in defects)

    # критерий без обязательного поля — тоже не проверка
    broken = await make_task(p.id, [{"type": "shell"}])
    verdict = await v.run(broken, p)
    ok, defects = verdict.ok, verdict.defects
    assert ok is False
    assert any("не запустится" in d for d in defects)

    # human честно говорит, что машина не судья
    human = await make_task(p.id, [{"type": "human", "criteria": "нравится дизайн"}])
    verdict = await v.run(human, p)
    ok, defects = verdict.ok, verdict.defects
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
    """Заглушка судьи. deterministic=1 по умолчанию — изображаем настоящую
    проверку, а не «модель посмотрела и одобрила»: иначе тест про ручную
    приёмку молча проверял бы совсем другой сценарий."""

    def __init__(self, ok=True, defects=None, assisted_only=False):
        self.ok = ok
        self.defects = defects or ["критерий не прошёл"]
        self.assisted_only = assisted_only
        self.calls = 0

    async def run(self, task, project):
        self.calls += 1
        det, ast = (0, 1) if self.assisted_only else (1, 0)
        return Verdict(ok=self.ok, defects=[] if self.ok else list(self.defects),
                       deterministic=det, assisted=ast)


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


# ---------- assisted != детерминированная приёмка ----------

def test_verdict_splits_evidence():
    """Вердикт различает, КТО его вынес: код или модель."""
    both = Verdict(ok=True, defects=[], deterministic=2, assisted=1)
    assert both.confirmed is True and both.assisted_only is False

    # одна модель и ничего больше — не приёмка
    soft = Verdict(ok=True, defects=[], deterministic=0, assisted=3)
    assert soft.confirmed is False and soft.assisted_only is True

    # провал остаётся провалом при любом составе проверок
    bad = Verdict(ok=False, defects=["сломано"], deterministic=1, assisted=1)
    assert bad.confirmed is False and bad.assisted_only is False


async def test_verifier_counts_by_source(db, tmp_path):
    """Счётчики набираются из реестра, а не из головы."""
    p = await make_project()
    (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
    async with Session() as s:
        pr = await s.get(Project, p.id)
        pr.workspace = str(tmp_path)
        await s.commit()
        p = await s.get(Project, p.id)

    task = await make_task(p.id, [
        {"type": "file_exists", "path": "style.css"},
        {"type": "dom", "url": JS_PAGE, "selector": "#static"},
    ])
    verdict = await Verifier().run(task, p)
    assert verdict.deterministic == 2 and verdict.assisted == 0
    assert verdict.confirmed is True


async def test_assisted_only_task_is_not_closed_automatically(db):
    """Задача, прошедшая только на слово модели, не становится done сама.

    Это главное отличие фазы 5.1: раньше вердикт по скриншоту попадал
    в статус задачи неотличимо от зелёного теста.
    """
    p = await make_project()
    task = await make_task(p.id, [{"type": "screenshot", "url": "https://x/",
                                   "criteria": "каталог в три колонки"}],
                           verify_class="assisted")

    comm = FakeCommunicator()
    sched = Scheduler(FakeExecutor(), FakeVerifier(assisted_only=True), comm)
    await sched.tick()
    await sched.drain()

    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == NEEDS_HUMAN, "assisted-приёмка закрыла задачу сама"
    assert row.attempts == 0, "это не провал: пересобирать нечего"
    assert manual.ASSISTED_ONLY in row.defects
    assert manual.waits_for_confirmation(row) is True
    # и клиенту про готовность не рапортуем, пока владелец не подтвердил
    assert not comm.done, "отчёт клиенту ушёл до подтверждения"
    # зато владельцу сказали, что от него ждут решения
    assert any(f"/confirm {task.id}" in n for n in comm.owner_notes)


async def test_deterministic_pass_still_closes_task(db):
    """Обратная сторона: настоящая проверка по-прежнему закрывает задачу сама."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "shell", "cmd": "echo ok"}])

    sched = Scheduler(FakeExecutor(), FakeVerifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()

    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == "done"


async def test_manual_submit_assisted_only_asks_for_confirmation(db):
    """И для человека правило то же: его слово плюс мнение модели — не критерий."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "llm", "criteria": "выглядит прилично"}],
                           executor="manual", verify_class="assisted")

    v = OkVerifier(ok=True, assisted_only=True)
    ok, defects = await manual.submit(task.id, v)

    assert ok is False, "приняли по одному мнению модели"
    assert any("подтвердить" in d for d in defects)
    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == NEEDS_HUMAN


async def test_confirm_closes_task_and_records_who(db):
    """Подтверждение владельца закрывает задачу, но остаётся в истории как
    подтверждение, а не как пройденная приёмка."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "llm", "criteria": "прилично"}],
                           executor="manual", verify_class="assisted")
    await manual.submit(task.id, OkVerifier(ok=True, assisted_only=True))

    ok, message = await manual.confirm(task.id)
    assert ok is True
    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == "done"
    assert "владельцем" in row.last_error, "не видно, что задачу закрыл человек"


async def test_confirm_refuses_live_task(db):
    """`/confirm` на работающей задаче — это прыжок через приёмку, а не приёмка."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "shell", "cmd": "true"}])

    ok, message = await manual.confirm(task.id)
    assert ok is False
    assert "подтверждать нечего" in message
    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == "ready"


def test_autonomy_counts_only_fully_autonomous():
    """Проверяемая машиной задача, которую делают руками, автономной не является."""
    tasks = [
        {"verify_class": "auto", "executor": "claude_code", "estimate_min": 10},
        # критерий машинный, но кликать в админке всё равно человеку
        {"verify_class": "auto", "executor": "manual", "estimate_min": 10},
    ]
    stats = autonomy(tasks)
    assert stats["autonomous"] == 1
    assert stats["by_class"]["auto"] == 2, "по классу их двое, а автономна одна"
    assert stats["auto_ratio"] == 0.5
