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
from autopilot.verifier import Verdict, Verifier, parse_judge_json

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


async def test_manual_show_lists_judge_observations(db):
    """Замечания вне критерия должны дойти до глаз владельца.

    Иначе они честно не влияют на приёмку и так же честно пропадают —
    а из них выходят следующие задачи.
    """
    p = await make_project()
    task = await make_task(p.id, [{"type": "shell", "cmd": "npm run build"}],
                           executor="manual", lane="build")
    async with Session() as s:
        row = await s.get(Task, task.id)
        row.observations = ["тестов на вебхук нет", "секрет шлюза лежит в коде"]
        await s.commit()
        row = await s.get(Task, task.id)

    text = manual.describe(row)
    assert "тестов на вебхук нет" in text
    assert "на приёмку не влияет" in text, "не видно, что это не дефект"


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
    assert both.confirmed is True and both.unproven is False

    # одна модель и ничего больше — не приёмка
    soft = Verdict(ok=True, defects=[], deterministic=0, assisted=3)
    assert soft.confirmed is False and soft.unproven is True

    # провал остаётся провалом при любом составе проверок
    bad = Verdict(ok=False, defects=["сломано"], deterministic=1, assisted=1)
    assert bad.confirmed is False and bad.unproven is False


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
    assert any(d.startswith(manual.NEEDS_CONFIRMATION) for d in row.defects)
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


# ---------- подозрительные критерии не считаются доказательством ----------

def test_verdict_suspicious_is_not_evidence():
    """Пустышка исполнена кодом, но доказательством не является."""
    v = Verdict(ok=True, defects=[], deterministic=0, assisted=1, suspicious=1)
    assert v.confirmed is False and v.unproven is True
    assert "пройдут и без этой задачи" in v.why_unproven()
    assert "проверок с моделью" in v.why_unproven()


async def test_suspicious_deterministic_does_not_close_task(db, tmp_path):
    """`http 200` на корне рядом со скриншотом больше не закрывает задачу.

    Ровно тот случай, что нашёлся на живом плане: детерминированная проверка
    перевешивала assisted по факту наличия, хотя не доказывала ничего.
    """
    p = await make_project()
    async with Session() as s:
        pr = await s.get(Project, p.id)
        pr.workspace = str(tmp_path)
        await s.commit()
        p = await s.get(Project, p.id)

    # единственная детерминированная проверка — селектор, который есть везде
    task = await make_task(p.id, [{"type": "dom", "url": JS_PAGE, "selector": "body"}])
    verdict = await Verifier().run(task, p)

    assert verdict.ok is True, "проверка сама по себе проходит"
    assert verdict.suspicious == 1 and verdict.deterministic == 0
    assert verdict.confirmed is False, "пустышка закрыла задачу"
    assert verdict.unproven is True


async def test_suspicious_only_task_goes_to_confirmation(db):
    """Задача с одними пустышками ждёт подтверждения, а не уходит в done."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "http", "url": "https://x/"}])

    comm = FakeCommunicator()
    sched = Scheduler(FakeExecutor(), FakeVerifier(suspicious_only=True), comm)
    await sched.tick()
    await sched.drain()

    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.status == NEEDS_HUMAN
    assert row.attempts == 0
    assert manual.waits_for_confirmation(row) is True
    assert not comm.done


async def test_one_real_check_still_counts(db, tmp_path):
    """Обратная сторона: непустая детерминированная проверка рядом с пустышкой
    доказательством остаётся."""
    p = await make_project()
    (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
    async with Session() as s:
        pr = await s.get(Project, p.id)
        pr.workspace = str(tmp_path)
        await s.commit()
        p = await s.get(Project, p.id)

    task = await make_task(p.id, [
        {"type": "dom", "url": JS_PAGE, "selector": "body"},        # пустышка
        {"type": "file_exists", "path": "style.css"},               # настоящая
    ])
    verdict = await Verifier().run(task, p)
    assert verdict.deterministic == 1 and verdict.suspicious == 1
    assert verdict.confirmed is True


# ---------- судья, начинающий ответ прозой ----------

def test_judge_json_plain():
    data, extracted = parse_judge_json('{"verdict": "PASS", "defects": []}')
    assert data["verdict"] == "PASS" and extracted is False

    data, extracted = parse_judge_json('```json\n{"verdict": "FAIL"}\n```')
    assert data["verdict"] == "FAIL" and extracted is False


def test_judge_json_after_prose():
    """Живой случай с проекта 2: сначала объяснение, потом вердикт."""
    raw = ("I need to analyze the diff provided, but I notice the diff is empty.\n"
           "Since no diff was provided, I cannot verify.\n\n"
           '{"verdict": "FAIL", "defects": ["нет изменений"]}')
    data, extracted = parse_judge_json(raw)
    assert extracted is True, "вердикт был в ответе, но не прочитан"
    assert data["verdict"] == "FAIL"
    assert data["defects"] == ["нет изменений"]


def test_judge_json_braces_inside_strings():
    """Скобка внутри текста дефекта не обрывает объект."""
    raw = 'ответ: {"verdict": "FAIL", "defects": ["сломан } тут", "и \\" кавычка"]}'
    data, extracted = parse_judge_json(raw)
    assert extracted is True
    assert data["defects"] == ["сломан } тут", 'и " кавычка']


def test_judge_json_truncated_stays_unreadable():
    """Оборванный на max_tokens ответ выдумывать не будем: это по-прежнему FAIL."""
    raw = 'I cannot verify.\n{"verdict": "FAIL", "defects": ['
    data, extracted = parse_judge_json(raw)
    assert data is None and extracted is False

    assert parse_judge_json("")[0] is None
    assert parse_judge_json("совсем без json")[0] is None


# ---------- судья отвечает на ЗАЯВЛЕННЫЙ вопрос, а не ревьюит проект ----------

# Живая задача 394 проекта 8. Критерий выполнен в дифе буквально, а судья
# вернул FAIL с девятью дефектами про middleware, идемпотентность, try/catch
# и отсутствие тестов. Замечания верные, но ни одно не про критерий: на живом
# проекте такая задача провалится MAX_ATTEMPTS раз и уедет в эскалацию.
CRIT_394 = ("В коде обработчика вебхука присутствует проверка подписи/секрета "
            "от платёжного шлюза и переход заказа в статус paid при успешном "
            "событии")


class FakeJudge:
    """Бэкенд, отдающий заранее заданный ответ судьи."""

    name = "cli"

    def __init__(self, payload):
        self.payload = payload
        self.prompts: list[str] = []

    async def ask(self, prompt, *, system="", model="", max_tokens=8000, content=None):
        from autopilot.llm import Reply
        self.prompts.append(prompt)
        body = (self.payload if isinstance(self.payload, str)
                else json.dumps(self.payload, ensure_ascii=False))
        return Reply(text=body, backend="cli")


def test_judge_prompt_does_not_presume_defects():
    """Формулировка «исходи из того, что дефекты есть» и толкала на ревью."""
    from autopilot.verifier import JUDGE_PROMPT

    assert "исходи из того" not in JUDGE_PROMPT.lower()
    assert "criterion_part" in JUDGE_PROMPT, "нечем привязать дефект к критерию"
    assert "observations" in JUDGE_PROMPT, "замечаниям вне критерия некуда деться"


def test_defect_attribution_needs_a_piece_of_the_criterion():
    """Как evidence в брифе: ссылка обязана указывать на что-то настоящее."""
    from autopilot.verifier import attribute_defects, part_of_criterion

    assert part_of_criterion("проверка подписи", CRIT_394) is True
    assert part_of_criterion("переход заказа в статус paid", CRIT_394) is True
    # склонение и порядок слов модель тасует свободно
    assert part_of_criterion("проверку подписи от шлюза", CRIT_394) is True
    # а это она придумала: в критерии нет ни слова про middleware
    assert part_of_criterion("middleware для raw body", CRIT_394) is False
    assert part_of_criterion("", CRIT_394) is False
    assert part_of_criterion("и", CRIT_394) is False, "предлог — не выдержка"

    kept, notes = attribute_defects(CRIT_394, {
        "defects": [
            {"defect": "нет middleware для raw body", "criterion_part": "raw body"},
            {"defect": "нет идемпотентности", "criterion_part": ""},
            {"defect": "статус не меняется", "criterion_part": "переход заказа в статус paid"},
            "дефект строкой, без привязки",
        ],
        "observations": ["тестов на вебхук нет"],
    })
    assert len(kept) == 1 and "статус не меняется" in kept[0]
    # ничего не потеряно: отброшенное переезжает в замечания
    assert len(notes) == 4
    assert any("тестов на вебхук нет" in n for n in notes)
    assert any("middleware" in n for n in notes)


def test_quoting_the_object_does_not_smuggle_a_review_defect():
    """Выдержка обязана быть НАРУШЕННЫМ ТРЕБОВАНИЕМ, а не упоминанием объекта.

    Первая версия проверки ловилась ровно на это: «обработчика вебхука» —
    настоящая часть критерия, и с ней в вердикт проходило любое замечание
    про то, как этот обработчик написан.
    """
    from autopilot.verifier import attribute_defects, defect_touches_criterion

    assert defect_touches_criterion("подпись не проверяется", CRIT_394) is True
    assert defect_touches_criterion("нет middleware для сырого тела", CRIT_394) is False

    kept, notes = attribute_defects(CRIT_394, {
        "verdict": "FAIL",
        "defects": [{"defect": "нет middleware для сырого тела запроса",
                     "criterion_part": "обработчика вебхука"}],
    })
    assert kept == []
    assert notes and "middleware" in notes[0]


async def test_judge_passes_when_criterion_met_but_code_imperfect(db):
    """Тот самый случай 394: критерий выполнен, прочие недостатки — в observations."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "llm", "criteria": CRIT_394}],
                           verify_class="assisted")
    fake = FakeJudge({
        "verdict": "FAIL",
        "defects": [
            {"defect": "нет middleware для сырого тела запроса",
             "criterion_part": "обработчика вебхука"},
            {"defect": "обработчик не идемпотентен", "criterion_part": ""},
            {"defect": "нет try/catch вокруг разбора тела", "criterion_part": ""},
        ],
        "observations": ["тестов на вебхук нет", "импорты в дифе не видны"],
    })

    verdict = await Verifier(backend=fake).run(task, p)

    assert verdict.ok is True, "выполненный критерий снова завален общим ревью"
    assert verdict.defects == []
    assert verdict.observations, "замечания судьи пропали совсем"
    assert any("идемпотент" in n for n in verdict.observations)
    assert any("тестов" in n for n in verdict.observations)
    # приёмка при этом остаётся недоказанной: судила модель
    assert verdict.unproven is True and verdict.confirmed is False

    async with Session() as s:
        row = await s.get(Task, task.id)
    assert row.observations, "замечания не дошли до владельца"


async def test_judge_still_fails_on_a_real_criterion_miss(db):
    """Обратная сторона: дефект, привязанный к критерию, по-прежнему валит."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "llm", "criteria": CRIT_394}],
                           verify_class="assisted")
    fake = FakeJudge({
        "verdict": "FAIL",
        "defects": [{"defect": "подпись не проверяется нигде",
                     "criterion_part": "проверка подписи/секрета"}],
        "observations": [],
    })

    verdict = await Verifier(backend=fake).run(task, p)
    assert verdict.ok is False
    assert any("подпись не проверяется" in d for d in verdict.defects)
    assert any("критерий:" in d for d in verdict.defects), "не видно, чем обосновано"


async def test_judge_pass_keeps_observations(db):
    """PASS не повод выбрасывать замечания: из них выйдут следующие задачи."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "llm", "criteria": CRIT_394}],
                           verify_class="assisted")
    fake = FakeJudge({"verdict": "PASS", "defects": [],
                      "observations": ["секрет шлюза лежит в коде"]})

    verdict = await Verifier(backend=fake).run(task, p)
    assert verdict.ok is True
    assert verdict.observations == ("секрет шлюза лежит в коде",)


async def test_judge_prompt_carries_the_criterion(db):
    """Судье уходит именно критерий задачи, а не её название."""
    p = await make_project()
    task = await make_task(p.id, [{"type": "llm", "criteria": CRIT_394}],
                           verify_class="assisted", title="Интеграция шлюза")
    fake = FakeJudge({"verdict": "PASS", "defects": [], "observations": []})
    await Verifier(backend=fake).run(task, p)

    assert CRIT_394 in fake.prompts[0]


def test_json_picked_by_key_not_by_position():
    """Из прозы берётся НУЖНЫЙ объект, а не первый попавшийся.

    Живой случай на плане проекта 8: модель начала объяснением, в котором
    был свой маленький объект. Вынули его, валидация сказала «tasks должен
    быть непустым списком», прогон ушёл на вторую попытку — а настоящий план
    лежал ниже в том же ответе. Тринадцать минут работы модели впустую.
    """
    raw = ('Сначала поясню формат: {"type": "shell", "cmd": "flutter build"}.\n'
           'Теперь сам план:\n'
           '{"stack_decision": {"chosen": ["Flutter"]}, '
           '"tasks": [{"title": "Каркас приложения"}]}')

    data, extracted = parse_judge_json(raw, require=("tasks",))
    assert extracted is True
    assert data["tasks"][0]["title"] == "Каркас приложения"

    # без ключа поведение прежнее: берём первый объект
    first, _ = parse_judge_json(raw)
    assert first["type"] == "shell"


def test_json_without_the_key_still_returned():
    """Если нужного ключа нет нигде, отдаём что нашли — отвергнет валидация.

    Молчаливое «ответа нет» тут хуже: оно прячет от нас настоящую причину.
    """
    raw = 'Не смог составить план.\n{"error": "мало данных"}'
    data, extracted = parse_judge_json(raw, require=("tasks",))
    assert data == {"error": "мало данных"} and extracted is True


def test_whole_answer_without_the_key_is_not_mistaken_for_a_fragment():
    """Чистый JSON без нужного ключа не должен молча превращаться в None."""
    data, extracted = parse_judge_json('{"verdict": "PASS"}', require=("tasks",))
    assert data == {"verdict": "PASS"}


async def test_judge_sees_brand_new_files(db, tmp_path):
    """Работа агента на пустом проекте — это НОВЫЕ файлы, и их надо показать.

    В `git diff` новые файлы не видны вовсе. На проекте, который начинают
    с нуля, судья получал бы пустой диф и честно отвечал «проверять нечего» —
    то есть первая же живая задача провалилась бы не по делу.
    """
    import subprocess

    repo = tmp_path / "workspace"
    repo.mkdir()
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "main.py").write_text("def handler():\n    return 42\n", encoding="utf-8")

    diff = await Verifier()._diff(str(repo))
    assert "main.py" in diff, "судья не увидел созданный агентом файл"
    assert "return 42" in diff, "в дифе нет содержимого новой работы"


async def test_diff_empty_outside_a_repo(db, tmp_path):
    """Не репозиторий — не диф. Молча брать чужой соседний репозиторий нельзя."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("hi", encoding="utf-8")
    assert await Verifier()._diff(str(plain)) == ""


async def test_executor_resolves_the_cmd_wrapper(db, monkeypatch, tmp_path):
    """Исполнитель обязан запускать claude.exe, а не обёртку claude.CMD.

    Первый живой прогон полосы build встал ровно на этом: npm на Windows
    ставит `claude.CMD`, `create_subprocess_exec` его не исполняет, и три
    задачи подряд ушли в эскалацию, не начав работу. В llm.py это починили
    ещё в фазе 9 — до исполнителя правка не дошла, потому что живьём его
    не запускали.
    """
    import asyncio as aio

    from autopilot.executor import Executor

    bin_dir = tmp_path / "npm"
    real = bin_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    real.mkdir(parents=True)
    (bin_dir / "claude.CMD").write_text("@echo off", encoding="utf-8")
    (real / "claude.exe").write_text("", encoding="utf-8")

    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = [str(a) for a in args]

        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                return (b'{"result": "ok", "total_cost_usd": 0.0, '
                        b'"session_id": "s1"}', b"")
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: str(bin_dir / "claude.CMD"))
    monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)

    p = await make_project()
    task = await make_task(p.id, [{"type": "shell", "cmd": "echo ok"}], lane="build")
    await Executor().run(task, p)

    assert seen["argv"][0].lower().endswith("claude.exe"), (
        f"исполнитель зовёт обёртку, а не бинарь: {seen['argv'][0]}")


async def test_executor_sends_prompt_via_stdin(db, monkeypatch):
    """Промпт исполнителя НЕ помещается в командную строку Windows.

    Предел — 32767 символов, а промпт содержит весь бриф: на проекте 8 это
    56 тысяч. CreateProcess падает с WinError 206, который Python отдаёт
    как FileNotFoundError, и первый живой прогон выглядел как «бинарь
    не найден», хотя бинарь был на месте. Ту же ловушку фаза 9 уже проходила
    на вызовах модели — до исполнителя правка не дошла.
    """
    import asyncio as aio

    from autopilot.executor import Executor

    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = [str(a) for a in args]
        seen["stdin"] = kwargs.get("stdin")

        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                seen["input"] = input
                return b'{"result": "ok", "total_cost_usd": 0.0}', b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)

    p = await make_project()
    async with Session() as s:
        row = await s.get(Project, p.id)
        # бриф размером с настоящий: именно он и переполнял командную строку
        row.brief = {"brief": {"deliverables": [{"text": "пункт " + "щ" * 400}
                                                for _ in range(100)]}}
        await s.commit()
        p = await s.get(Project, p.id)

    task = await make_task(p.id, [{"type": "shell", "cmd": "echo ok"}], lane="build")
    await Executor().run(task, p)

    joined = " ".join(seen["argv"])
    assert len(joined) < 32767, "командная строка снова длиннее предела Windows"
    assert "пункт" not in joined, "промпт уехал в аргументы, а не в stdin"
    assert seen["input"] and b"\xd0\xbf\xd1\x83\xd0\xbd\xd0\xba\xd1\x82" in seen["input"], (
        "промпт не дошёл до агента через stdin")


async def test_failed_session_is_still_charged_and_explained(db, monkeypatch):
    """Сессия, упёршаяся в потолок ходов, стоит денег и обязана их показать.

    Первый живой прогон: три сессии исчерпали MAX_TURNS, сожгли $9.39 квоты
    по счёту самого CLI — и оставили ОТКРЫТЫЕ строки с нулём. Суточный
    потолок этих денег не увидел. В лог при этом уезжало голое «None»:
    CLI на исчерпании ходов отдаёт `result: null`.
    """
    import asyncio as aio

    from autopilot.db import Run
    from autopilot.executor import Executor

    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                return (b'{"is_error": true, "result": null, "num_turns": 41, '
                        b'"stop_reason": "tool_use", "total_cost_usd": 2.74}', b"")
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)

    p = await make_project()
    task = await make_task(p.id, [{"type": "shell", "cmd": "echo ok"}], lane="build")

    with pytest.raises(RuntimeError) as exc:
        await Executor().run(task, p)
    assert "потолок ходов" in str(exc.value), f"причина скрыта: {exc.value}"

    async with Session() as s:
        row = (await s.execute(select(Run))).scalars().one()
    assert row.finished_at is not None, "строка расхода осталась открытой"
    assert row.ok is False
    assert row.cost_usd == 2.74, "сожжённая квота не записана"
    assert row.cost_estimated is False, "это замер CLI, а не наша оценка"
