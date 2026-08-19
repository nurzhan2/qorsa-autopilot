"""Фаза 9: API и CLI как взаимозаменяемые бэкенды, упор в квоту подписки.

Главное здесь — упор в квоту НЕ ЕСТЬ провал работы. Раньше любой сбой вызова
означал неудачную попытку, и четыре упора подряд увели бы живую задачу
в эскалацию, а бриф обнулили бы.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FakeCommunicator, make_project, make_tasks

from autopilot import brief as B, limits, llm
from autopilot.config import cfg
from autopilot.db import Project, Run, Session, Task
from autopilot.fakes import FakeExecutor, FakeVerifier
from autopilot.llm import CliBackend, LimitReached, LLMError, Reply
from autopilot.scheduler import Scheduler


@pytest.fixture(autouse=True)
def clean_limits():
    """Состояние квоты общее на процесс — между тестами его надо сбрасывать."""
    limits.state.clear()
    yield
    limits.state.clear()


# ---------- выбор бэкенда ----------

def test_backend_per_consumer(monkeypatch):
    """Каждый потребитель настраивается отдельно: судью можно держать
    на подписке, а бриф вернуть на API, когда там появятся деньги."""
    monkeypatch.setattr(cfg, "llm_backend", "cli")
    monkeypatch.setattr(cfg, "llm_backend_brief", "api")
    monkeypatch.setattr(cfg, "llm_backend_plan", "")
    monkeypatch.setattr(cfg, "llm_backend_judge", "cli")

    assert llm.backend_for("brief") == llm.API
    assert llm.backend_for("plan") == llm.CLI      # падает на общий LLM_BACKEND
    assert llm.backend_for("judge") == llm.CLI
    assert isinstance(llm.make("plan"), llm.CliBackend)
    assert isinstance(llm.make("brief"), llm.ApiBackend)


def test_unknown_backend_falls_back_to_cli(monkeypatch):
    """Опечатка в настройке не должна ронять процесс и не должна тратить деньги."""
    monkeypatch.setattr(cfg, "llm_backend", "апи")
    monkeypatch.setattr(cfg, "llm_backend_plan", "")
    assert llm.backend_for("plan") == llm.CLI


# ---------- распознавание упора ----------

def test_limit_detection():
    """Упор отличается от ошибки по тексту И по коду возврата."""
    for text in ("Usage limit reached", "5-hour limit exceeded, resets at 4pm",
                 "rate limit", "Too Many Requests", "лимит исчерпан"):
        assert llm.looks_like_limit(text) is True, text

    for text in ("command not found", "invalid api key", "syntax error"):
        assert llm.looks_like_limit(text) is False, text

    # 429 приходит и кодом возврата
    assert llm.looks_like_limit("", 429) is True
    assert llm.looks_like_limit("", 1) is False


def test_retry_after_parsed():
    assert llm.retry_after_from("limit reached, resets in 2h 30m") == 2 * 3600 + 30 * 60
    assert llm.retry_after_from("resets at 45m") == 45 * 60
    assert llm.retry_after_from("нет времени тут") is None


# ---------- состояние квоты ----------

def test_limit_state_backoff_grows(monkeypatch):
    """Без указанного времени сброса пауза растёт: ломиться в закрытую дверь
    раз в минуту — способ сжечь остаток окна на пустые попытки."""
    monkeypatch.setattr(cfg, "limit_backoff_start_sec", 100)
    monkeypatch.setattr(cfg, "limit_backoff_max_sec", 400)
    st = limits.LimitState()

    assert st.hit("нет квоты", now=0) == 100
    assert st.hit("нет квоты", now=0) == 200
    assert st.hit("нет квоты", now=0) == 400
    assert st.hit("нет квоты", now=0) == 400, "пауза должна упереться в потолок"

    # CLI сказал время сброса — верим ему и сбрасываем рост
    assert st.hit("resets", retry_after=60, now=0) == 60


def test_limit_state_notifies_once_per_period(monkeypatch):
    """Уведомление на каждый вызов превращает телефон в будильник."""
    monkeypatch.setattr(cfg, "limit_notify_every_sec", 1000)
    st = limits.LimitState()
    assert st.should_notify(now=0) is True
    assert st.should_notify(now=100) is False
    assert st.should_notify(now=900) is False
    assert st.should_notify(now=1100) is True


def test_limit_state_clears():
    st = limits.LimitState()
    st.hit("нет квоты", retry_after=100, now=0)
    assert st.blocked(now=50) is True
    st.clear()
    assert st.blocked(now=50) is False and st.hits == 0


# ---------- ГЛАВНОЕ: упор не уводит задачу в эскалацию ----------

class LimitedVerifier:
    """Приёмщик, который всегда упирается в квоту."""

    def __init__(self):
        self.calls = 0

    async def run(self, task, project):
        self.calls += 1
        raise LimitReached("Usage limit reached, resets in 1h", retry_after=3600)


async def test_three_limits_do_not_grow_attempts(db, monkeypatch):
    """Три упора подряд: attempts не растёт, эскалации нет, задача жива.

    Это главный тест фазы. Раньше упор считался провалом, и четвёртый
    подряд увёл бы рабочую задачу в escalated с блокировкой проекта.
    """
    monkeypatch.setattr(cfg, "max_attempts", 2)      # эскалация была бы уже на 2-м
    p = await make_project()
    ids = await make_tasks(p.id, 1, lane="verify")

    ver = LimitedVerifier()
    comm = FakeCommunicator()
    sched = Scheduler(FakeExecutor(), ver, comm)

    for _ in range(3):
        limits.state.clear()          # окно «открылось» — пробуем снова
        await sched.tick()
        await sched.drain()

    async with Session() as s:
        row = await s.get(Task, ids[0])
        proj = await s.get(Project, p.id)

    assert ver.calls == 3, "верификатор не вызывался — тест ничего не проверил"
    assert row.attempts == 0, f"упор засчитан как попытка: attempts={row.attempts}"
    assert row.status == "ready", f"задача не вернулась в очередь: {row.status}"
    assert row.defects in ([], None), "упор записан как дефект работы"
    assert proj.status != "blocked", "проект заблокирован из-за квоты"


async def test_limit_stops_all_paid_lanes(db):
    """Квота одна на процесс: встают обе платные полосы, chat работает."""
    p = await make_project()
    await make_tasks(p.id, 2, lane="build")
    await make_tasks(p.id, 2, lane="chat")

    ex, comm = FakeExecutor(), FakeCommunicator()
    sched = Scheduler(ex, FakeVerifier(), comm)

    limits.state.hit("квота", retry_after=3600)
    await sched.tick()
    await sched.drain()

    assert ex.served == [], "build работал при закрытой квоте"
    assert comm.processed, "chat встал вместе с платными полосами"


async def test_owner_notified_once_about_limit(db):
    """Про упор владельцу сообщают один раз за период, а не на каждый вызов."""
    p = await make_project()
    await make_tasks(p.id, 3, lane="verify")

    comm = FakeCommunicator()
    sched = Scheduler(FakeExecutor(), LimitedVerifier(), comm)

    # Окно НЕ восстанавливается между тиками — это один инцидент, и сообщить
    # о нём надо один раз. Разблокируем только флаг паузы, чтобы верификатор
    # вызвался снова и снова упёрся
    for _ in range(3):
        limits.state.blocked_until = 0.0
        await sched.tick()
        await sched.drain()

    assert len(comm.owner_notes) == 1, (
        f"уведомлений {len(comm.owner_notes)}, ожидалось одно")
    assert "квот" in comm.owner_notes[0].lower()
    assert "не провалены" in comm.owner_notes[0]


async def test_limit_does_not_wipe_brief(db, monkeypatch):
    """Бриф при упоре не обнуляется и не считается несобранным."""
    from autopilot import brief as B

    p = await make_project()
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.brief = {"brief": {"goal": {"text": "накопленное", "evidence": ["1"]},
                               "deliverables": [{"text": "пункт", "evidence": ["1"],
                                                 "priority": "must"}]}}
        row.brief_ready = True
        await s.commit()
        project = await s.get(Project, p.id)

    # без сообщений build() вернул бы прежний бриф, не дойдя до модели
    from autopilot.db import ChatMessage
    async with Session() as s:
        s.add(ChatMessage(transport="telegram", chat_id="c1", tg_message_id="1",
                          project_id=p.id, direction="in", sender_id="42",
                          sender_role="client", text="нужен каталог с фильтрами"))
        await s.commit()

    brief = B.Brief(communicator=FakeCommunicator())

    async def limited(*a, **k):
        raise LimitReached("Usage limit reached", retry_after=1800)
    monkeypatch.setattr(brief, "_attempt", limited)

    with pytest.raises(LimitReached):
        await brief.build(project)

    async with Session() as s:
        row = await s.get(Project, p.id)
    body = (row.brief or {}).get("brief") or {}
    assert body.get("goal"), "накопленный бриф стёрт упором в квоту"
    assert len(body.get("deliverables") or []) == 1
    assert row.status != "blocked", "проект заблокирован из-за квоты"


# ---------- CLI-бэкенд ----------

def test_cli_never_resumes_and_has_no_tools(monkeypatch):
    """Судья обязан быть свежей сессией, а инструменты выключены полностью."""
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["cwd"] = kwargs.get("cwd")

        class P:
            returncode = 0

            async def communicate(self, input=None):
                return (b'{"result": "{\\"verdict\\": \\"PASS\\"}", '
                        b'"total_cost_usd": 0.01, "usage": {}}', b"")
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    reply = asyncio.run(CliBackend().ask("вопрос", system="правила"))

    args = list(seen["args"])
    assert "--resume" not in args, "судья продолжил чужую сессию — он оценит свою работу"
    assert "--max-turns" in args and args[args.index("--max-turns") + 1] == "1"
    assert args[args.index("--allowedTools") + 1] == "", "инструменты не отключены"
    # --disallowedTools НЕ передаём: перечисление инструментов по именам
    # подтягивает их описания и учетверяет расход кэша (26k против 6.7k)
    assert "--disallowedTools" not in args
    # системный промпт ЗАМЕНЯЕТСЯ, а не дописывается, и динамические секции
    # выключены — иначе каждый вызов создаёт ~40k токенов кэша впустую
    assert "--system-prompt" in args and "--append-system-prompt" not in args
    assert "--exclude-dynamic-system-prompt-sections" in args
    assert args[args.index("--model") + 1] == cfg.cli_model
    assert "opus" not in " ".join(args).lower(), "поднят Opus — квота нужна владельцу"
    # каталог временный: в репозитории проекта CLI подхватил бы чужой CLAUDE.md
    assert "qorsa-llm-" in str(seen["cwd"])
    assert reply.backend == llm.CLI and reply.billed is False


def test_cli_missing_binary_says_what_to_do(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *a: None)
    with pytest.raises(LLMError, match="не нашёл"):
        asyncio.run(CliBackend().ask("вопрос"))


def test_cli_limit_becomes_limit_reached(monkeypatch):
    """Упор в квоту приходит как LimitReached, а не как обычная ошибка."""
    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 1

            async def communicate(self, input=None):
                return (b"", "Usage limit reached, resets in 3h".encode())
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(LimitReached) as exc:
        asyncio.run(CliBackend().ask("вопрос"))
    assert exc.value.retry_after == 3 * 3600


def test_cli_refuses_images_instead_of_judging_blind(monkeypatch):
    """Картинки CLI не принимает. Молча судить скриншот, не увидев его,
    было бы приёмкой на пустом месте."""
    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    with pytest.raises(LLMError, match="картинки"):
        asyncio.run(CliBackend().ask("", content=[{"type": "image"}]))


# ---------- раздельный учёт ----------

async def test_cli_spending_does_not_touch_money_budget(db):
    """Подписка не считается деньгами: иначе бюджет встанет на пустом месте."""
    from autopilot.db import consumed_today, spent_today

    p = await make_project()
    ids = await make_tasks(p.id, 1)
    async with Session() as s:
        s.add(Run(task_id=ids[0], kind="brief", ok=True, backend="cli",
                  cost_usd=9.99, seconds=30))
        s.add(Run(task_id=ids[0], kind="judge", ok=True, backend="api",
                  cost_usd=0.25, seconds=5))
        await s.commit()

    assert await spent_today() == pytest.approx(0.25), (
        "оценка подписки попала в суточный бюджет реальных денег")

    both = await consumed_today()
    assert both["api_usd"] == pytest.approx(0.25)
    assert both["cli_usd_est"] == pytest.approx(9.99)
    assert both["cli_calls"] == 1 and both["api_calls"] == 1


def test_reply_knows_whether_it_was_billed():
    assert Reply(text="x", backend=llm.API).billed is True
    assert Reply(text="x", backend=llm.CLI).billed is False


# ---------- окно подписки против ручной работы ----------

def test_build_quiet_hours(monkeypatch):
    """Headless-сессии можно развести с ручной работой владельца по времени."""
    import datetime as dt

    monkeypatch.setattr(cfg, "quiet_build_start", 9)
    monkeypatch.setattr(cfg, "quiet_build_end", 19)
    at = lambda h: dt.datetime(2026, 8, 16, h, 0)          # noqa: E731

    assert Scheduler.build_allowed(at(3)) is True          # ночью можно
    assert Scheduler.build_allowed(at(12)) is False        # днём окно нужно мне
    assert Scheduler.build_allowed(at(19)) is True

    # равные границы выключают ограничение
    monkeypatch.setattr(cfg, "quiet_build_start", 0)
    monkeypatch.setattr(cfg, "quiet_build_end", 0)
    assert Scheduler.build_allowed(at(12)) is True


async def test_cc_max_concurrent_caps_both_lanes(db, monkeypatch):
    """CC_MAX_CONCURRENT ограничивает СЕССИИ, а не задачи одной полосы.

    build и verify оба поднимают процессы claude и вместе съедают окно
    быстрее, чем каждая полоса по отдельности.
    """
    monkeypatch.setattr(cfg, "cc_max_concurrent", 1)
    sched = Scheduler(FakeExecutor(), FakeVerifier(), FakeCommunicator())

    assert sched.free_slots("build") == 1
    sched.running["verify"] = 1                # приёмка заняла единственную сессию
    assert sched.free_slots("build") == 0, "build полез поверх лимита сессий"
    # chat к Claude Code не ходит — его этот потолок не касается
    assert sched.free_slots("chat") == cfg.lane_limits["chat"]


# ---------- живой CLI: то, на чём он падал ----------

def test_prompt_goes_via_stdin_not_argv(monkeypatch):
    """Промпт передаётся В STDIN, а не аргументом командной строки.

    На Windows npm ставит claude как .CMD, запуск шёл через cmd.exe, а у него
    командная строка ограничена 8191 символом. Промпт брифа — пятнадцать тысяч,
    и вызов падал с обрубленной абракадаброй вместо внятной ошибки: cmd ещё
    и калечил кириллицу.
    """
    seen = {}
    long_prompt = "Требования клиента. " * 2000          # ~40 тысяч символов

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        seen["stdin"] = kwargs.get("stdin")

        class P:
            returncode = 0

            async def communicate(self, input=None):
                seen["input"] = input
                return (b'{"result": "ok", "total_cost_usd": 0.01, "usage": {}}', b"")
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(CliBackend().ask(long_prompt))

    assert long_prompt not in " ".join(str(a) for a in seen["args"]), (
        "длинный промпт уехал в argv — упрётся в лимит командной строки")
    assert seen["input"] == long_prompt.encode("utf-8"), "промпт не ушёл в stdin"
    assert seen["stdin"] is not None


def test_windows_cmd_wrapper_resolved_to_real_exe(monkeypatch, tmp_path):
    """`claude.CMD` — обёртка, которую CreateProcess запустить не может.

    Рядом лежит настоящий claude.exe: берём его и обходимся без cmd.exe,
    у которого и лимит строки, и порча кодировки.
    """
    from autopilot import llm as L

    npm = tmp_path / "npm"
    real = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    real.mkdir(parents=True)
    (real / "claude.exe").write_text("", encoding="utf-8")
    wrapper = npm / "claude.CMD"
    wrapper.write_text("@echo off", encoding="utf-8")

    monkeypatch.setattr("shutil.which", lambda *a: str(wrapper))
    resolved, launcher = L.resolve_cli("claude")
    assert resolved.endswith("claude.exe") and launcher == [], (
        "не нашли настоящий бинарь — пойдём через cmd.exe с его лимитами")

    # настоящего бинаря нет — деваться некуда, идём через интерпретатор
    (real / "claude.exe").unlink()
    resolved, launcher = L.resolve_cli("claude")
    assert resolved == str(wrapper) and launcher and "cmd" in launcher[0].lower()


def test_reply_reports_actual_model(monkeypatch):
    """Какая модель отвечала НА САМОМ ДЕЛЕ, а не какую мы просили.

    CLI по умолчанию берёт Opus, чья недельная квота на порядок меньше
    и нужна владельцу. Проверять надо по факту.
    """
    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 0

            async def communicate(self, input=None):
                return (b'{"result": "ok", "total_cost_usd": 0.05, '
                        b'"usage": {"cache_creation_input_tokens": 7000}, '
                        b'"modelUsage": {"claude-sonnet-5": {}}}', b"")
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    reply = asyncio.run(CliBackend().ask("вопрос"))
    assert reply.models == ("claude-sonnet-5",)
    assert "sonnet" in reply.model_names and "opus" not in reply.model_names
    assert reply.cache_creation_tokens == 7000


def test_disallowed_tools_never_passed(monkeypatch):
    """--disallowedTools стоит вчетверо больше квоты, и это незаметно.

    Перечисление инструментов по именам подтягивает в контекст их описания:
    26k токенов кэша на вызов против 6.7k без него. Замерено на живом CLI.
    Запрещать поимённо то, что и так не разрешено пустым allowlist, — значит
    платить за список запретов. Отдельный тест, а не строка в общем: вернуть
    флаг «для надёжности» легко, а замечает это только счёт за квоту.
    """
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"] = args

        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                return b'{"result": "ok", "usage": {}}', b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    for consumer in ("brief", "plan", "judge"):
        seen.clear()
        asyncio.run(CliBackend().ask(f"вопрос от {consumer}"))
        args = [str(a) for a in seen["args"]]
        assert "--disallowedTools" not in args, (
            f"вернулся --disallowedTools ({consumer}): расход кэша вырастет вчетверо")
        # и запрет по-прежнему держится пустым allowlist, а не отсутствием флагов
        assert args[args.index("--allowedTools") + 1] == ""


# ---------- расход виден с НАЧАЛА вызова ----------

async def test_run_row_exists_before_the_model_answers(db):
    """Строка расхода заводится до вызова, а не после.

    Раньше `started_at` был на самом деле временем ОКОНЧАНИЯ: строка
    создавалась после ответа. Прогон, убитый на середине, не оставлял следа
    вовсе — квоту сжёг, а в учёте пусто.
    """
    from sqlalchemy import select

    from autopilot.brief import Brief
    from autopilot.db import Run, utcnow

    project = await make_project()
    seen = {}

    class SlowBackend:
        name = "cli"

        async def ask(self, prompt, *, system="", model="", max_tokens=8000,
                      content=None):
            async with Session() as s:
                rows = (await s.execute(select(Run))).scalars().all()
                seen["во время вызова"] = [(r.kind, r.ok, r.finished_at) for r in rows]
            return Reply(text='{"goal": null, "deliverables": [], "confidence": 0.1}',
                         backend="cli", seconds=1.0, cost_usd=0.02)

    brief = Brief(backend=SlowBackend())
    before = utcnow()
    await brief._call("промпт", None, project.id)

    assert len(seen["во время вызова"]) == 1, "строки расхода не было во время вызова"
    kind, ok, finished = seen["во время вызова"][0]
    assert kind == "brief" and ok is False and finished is None, (
        "незакрытая строка — это и есть признак «вызов ещё не вернулся»")

    async with Session() as s:
        row = (await s.execute(select(Run))).scalars().one()
    assert row.ok is True and row.finished_at is not None
    assert row.started_at >= before and row.started_at <= row.finished_at, (
        "started_at снова оказался временем окончания")
    assert row.backend == "cli" and row.cost_usd == 0.02


async def test_aborted_call_is_recorded_at_zero_cost(db):
    """Оборванный вызов не имеет подтверждённой цены — cost_usd=0.

    Раньше тут была оценка по времени: цифру расхода отдаёт CLI вместе
    с ответом, а у срубленного вызова ответа нет, значит «сжёг примерно
    N долларов». Сама оценка оказалась багом — сон машины во время вызова
    засчитывался как минуты работы, и на живом прогоне это дало фантомные
    $30+ на одной убитой задаче. Полезной работы не подтверждено — значит
    и цена не подтверждена: пишем 0, а не гадаем.
    """
    from sqlalchemy import select

    from autopilot.brief import Brief
    from autopilot.db import Run

    project = await make_project()

    class DeadBackend:
        name = "cli"

        async def ask(self, *a, **kw):
            raise LimitReached("окно кончилось")

    with pytest.raises(LimitReached):
        await Brief(backend=DeadBackend())._call("промпт", None, project.id)

    async with Session() as s:
        row = (await s.execute(select(Run))).scalars().one()
    assert row.kind == "brief" and row.ok is False
    assert row.cost_usd == 0.0, "оборванный вызов не должен нести придуманную цену"
    assert row.cost_estimated is True, "это не замер CLI — пометка должна остаться"


def test_answer_mentioning_rate_limits_is_not_a_quota_wall(monkeypatch):
    """Слово «лимит» В ОТВЕТЕ модели — не упор в квоту.

    Живой случай, стоивший 26 минут работы модели: план приложения доставки
    честно расписал rate limit для API, а `looks_like_limit` обыскивал весь
    stdout — то есть сам ответ. Прогон объявили упором в квоту, план выбросили,
    и по правилам limits.py встала бы вся работа разом. При этом в конверте
    стояло `is_error:false` и `stop_reason:end_turn`: модель ответила успешно.

    Про исчерпание квоты нам говорит оболочка, а не содержимое ответа.
    """
    plan = ("Настроить rate limit на API: не больше 100 запросов в минуту. "
            "Квота внешнего сервиса превышена не будет.")

    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                body = json.dumps({"is_error": False, "stop_reason": "end_turn",
                                   "result": plan, "total_cost_usd": 1.79,
                                   "usage": {"output_tokens": 100668}})
                return body.encode(), b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    reply = asyncio.run(CliBackend().ask("составь план"))
    assert reply.text == plan, "успешный ответ принят за упор в квоту"
    assert reply.output_tokens == 100668


def test_real_quota_wall_is_still_detected(monkeypatch):
    """Обратная сторона: настоящий упор по-прежнему опознаётся."""
    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 1
            pid = None

            async def communicate(self, input=None):
                body = json.dumps({"is_error": True,
                                   "result": "Usage limit reached, resets in 2h"})
                return body.encode(), b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(LimitReached) as exc:
        asyncio.run(CliBackend().ask("вопрос"))
    assert exc.value.retry_after == 2 * 3600


def test_failed_call_with_real_generation_is_not_a_quota_wall(monkeypatch):
    """`is_error:true` С реальной генерацией — обычная ошибка, а не упор.

    Одной починки формулировки мало: план, упомянувший «rate limit» в СВОЁМ
    тексте, мог оборваться на чём угодно (потолок выходных токенов, например)
    и всё равно попасть в `result` при `is_error:true` — под старой логикой
    это снова читалось бы как упор в квоту. Настоящий упор отличается тем,
    что генерации не было ВООБЩЕ: ни стоимости, ни токенов вывода. Здесь они
    есть — значит модель честно работала и просто не завершила ответ, а не
    «подожди» от CLI.
    """
    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 1
            pid = None

            async def communicate(self, input=None):
                body = json.dumps({
                    "is_error": True,
                    "result": ("Настроить rate limit на API: не больше 100 "
                              "запросов в минуту к внешнему сервису."),
                    "total_cost_usd": 0.42,
                    "usage": {"output_tokens": 32000},
                })
                return body.encode(), b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(LLMError):
        asyncio.run(CliBackend().ask("составь план"))


# ---------- второй тормоз: расход подписки ----------

async def test_subscription_budget_stops_build(db, monkeypatch):
    """У build должен остаться ограничитель и после перехода на подписку.

    Раньше исполнитель писал свой расход как `backend="api"`, и его держал
    денежный потолок. Это было неправдой: сессия Claude Code денег не тратит.
    Стоило записать её честно — и у полосы не осталось бы вообще никакого
    тормоза, потому что `spent_today()` на подписке всегда ноль.
    """
    from autopilot.db import cli_spent_today, spent_today
    from conftest import make_tasks

    p = await make_project()
    await make_tasks(p.id, 2, lane="build")
    await make_tasks(p.id, 2, lane="verify", start_idx=10)
    await make_tasks(p.id, 2, lane="chat", start_idx=20)

    monkeypatch.setattr(cfg, "daily_cli_budget_usd", 1.0)
    async with Session() as s:
        s.add(Run(task_id=1, kind="execute", ok=True, backend="cli", cost_usd=5.0))
        await s.commit()

    assert await spent_today() == 0.0, "подписка просочилась в денежный счётчик"
    assert await cli_spent_today() == 5.0

    ex, ver, comm = FakeExecutor(), FakeVerifier(), FakeCommunicator()
    sched = Scheduler(ex, ver, comm)
    await sched.tick()
    await sched.drain()

    assert sched.budget_paused is True
    assert ex.calls == [], "build поехал при исчерпанном потолке подписки"
    assert ver.calls == [], "verify поехал при исчерпанном потолке подписки"
    assert comm.processed, "chat должен работать всегда"


async def test_money_budget_untouched_by_subscription(db, monkeypatch):
    """Обратная сторона: расход подписки не съедает денежный бюджет.

    Иначе суточный бюджет вставал бы на пустом месте — всё идёт по подписке,
    не потрачено ни рубля, а работа остановлена.
    """
    from conftest import make_tasks

    p = await make_project()
    await make_tasks(p.id, 2, lane="build")

    monkeypatch.setattr(cfg, "daily_budget_usd", 1.0)
    monkeypatch.setattr(cfg, "daily_cli_budget_usd", 1000.0)
    async with Session() as s:
        s.add(Run(task_id=1, kind="execute", ok=True, backend="cli", cost_usd=50.0))
        await s.commit()

    ex = FakeExecutor()
    sched = Scheduler(ex, FakeVerifier(), FakeCommunicator())
    await sched.tick()
    await sched.drain()

    assert sched.budget_paused is False, "подписка засчитана как деньги"
    assert ex.calls, "build встал, хотя денег не потрачено"


async def test_executor_writes_subscription_not_money(db):
    """Строка расхода исполнителя помечена подпиской, а не деньгами."""
    from autopilot import executor as ex_mod

    assert ex_mod.RUN_BACKEND == "cli", (
        "сессия Claude Code идёт по подписке — писать её как деньги значит "
        "врать в обе стороны сразу")


async def test_limit_stops_dispatch_inside_the_same_tick(db, monkeypatch):
    """Упор в квоту останавливает выдачу немедленно, а не со следующего тика.

    Гонка, найденная плавающим тестом: `_on_limit` возвращает задачу в очередь
    как `ready`, и тот же самый проход `_dispatch` подхватывает её снова —
    в уже закрытое окно. Три тика давали четыре вызова судьи.

    В бою это опаснее, чем красный тест: правило «упёрлись — встала вся
    работа» переставало работать ровно в тот момент, ради которого писалось,
    и остаток окна сгорал на заведомо пустых попытках.
    """
    from conftest import make_tasks

    p = await make_project()
    await make_tasks(p.id, 3, lane="verify")

    ver = LimitedVerifier()
    sched = Scheduler(FakeExecutor(), ver, FakeCommunicator())
    await sched.tick()
    await sched.drain()

    assert ver.calls == 1, (
        f"после упора в квоту в том же тике сделано ещё {ver.calls - 1} "
        f"вызовов в закрытое окно")


async def test_service_task_is_reused_not_bred(db):
    """Якорь для расхода — один на проект, а не по одному на каждый вызов.

    На проекте 8 их накопилось 35 штук: по одной служебной задаче на каждое
    обращение к модели. Расход всё равно живёт в `Run`, задача нужна только
    затем, чтобы было к чему его привязать.
    """
    from sqlalchemy import select

    from autopilot.brief import Brief
    from autopilot.db import Run

    project = await make_project()

    class Quiet:
        name = "cli"

        async def ask(self, prompt, *, system="", model="", max_tokens=8000,
                      content=None):
            return Reply(text="{}", backend="cli", seconds=1.0, cost_usd=0.01)

    brief = Brief(backend=Quiet())
    for _ in range(3):
        await brief._call("промпт", None, project.id)

    async with Session() as s:
        anchors = (await s.execute(
            select(Task).where(Task.project_id == project.id,
                               Task.lane == "chat"))).scalars().all()
        runs = (await s.execute(select(Run))).scalars().all()

    assert len(anchors) == 1, f"на три вызова заведено {len(anchors)} служебных задач"
    assert len(runs) == 3, "а вот строк расхода должно быть по одной на вызов"
    assert {r.task_id for r in runs} == {anchors[0].id}


async def test_aborted_calls_do_not_move_the_subscription_brake(db, monkeypatch):
    """Обратная сторона: оборванные вызовы БОЛЬШЕ НЕ двигают суточный потолок.

    Прежняя версия этого теста проверяла противоположное — что оценка по
    времени доходит до счётчика подписки, и это было осознанным решением.
    Решение развернули: на живом прогоне тот же механизм посчитал часы сна
    машины как работу и накрутил фантомные $30+ за одну убитую задачу,
    упёршись в потолок раньше настоящих трат. Тормоз для реального упора
    в квоту — отдельный механизм (`limits.py`, разбирает текст ответа CLI),
    а не накопленная оценка по секундам.
    """
    from autopilot.db import cli_spent_today

    project = await make_project()

    class Dead:
        name = "cli"

        async def ask(self, *a, **kw):
            raise LLMError("не ответил за 1800s")

    brief = B.Brief(backend=Dead())
    for _ in range(2):
        with pytest.raises(LLMError):
            await brief._call("промпт", None, project.id)

    spent = await cli_spent_today()
    assert spent == pytest.approx(0.0), (
        f"оборванные вызовы сдвинули счётчик на {spent:.2f} — цена не должна была быть придумана")


def test_real_session_limit_text_is_recognised():
    """Живой текст CLI, на котором упор приняли за поломку.

    «You've hit your session limit · resets 5:50pm (Asia/Qyzylorda)» не подошёл
    ни под один маркер: там «session limit», а не «usage limit», и «resets
    5:50pm», а не «resets at». Прогон плана объявили недоступностью модели —
    а в бою это стоило бы задаче засчитанной попытки и дороги в эскалацию.
    """
    import datetime as dt

    text = "You've hit your session limit · resets 5:50pm (Asia/Qyzylorda)"
    assert llm.looks_like_limit(text) is True, "настоящий упор принят за поломку"

    # и пауза считается до названного времени, а не с потолка
    now = dt.datetime(2026, 8, 16, 17, 38)
    assert llm.retry_after_from(text, now=now) == 12 * 60

    # время уже прошло — значит окно откроется завтра
    late = dt.datetime(2026, 8, 16, 18, 10)
    assert llm.retry_after_from(text, now=late) == pytest.approx(23 * 3600 + 40 * 60)

    # прежняя форма «через сколько» продолжает работать
    assert llm.retry_after_from("Usage limit reached, resets in 3h") == 3 * 3600


def test_limit_text_only_checked_on_failure(monkeypatch):
    """Слово resets в УСПЕШНОМ ответе упором не считается.

    Маркеры стали шире, и это безопасно ровно потому, что успешный конверт
    мы больше не обыскиваем: разбор идёт до проверки.
    """
    plan = "Задача: настроить сброс пароля, resets по ссылке из письма"

    async def fake_exec(*args, **kwargs):
        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                return json.dumps({"is_error": False, "result": plan,
                                   "usage": {}}).encode(), b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(CliBackend().ask("план")).text == plan


def test_cli_gets_the_token_ceiling_through_env(monkeypatch):
    """Потолок ответа у CLI задаётся переменной окружения, а не флагом.

    Записано было обратное — «CLI не принимает max_tokens вовсе», — и наши
    BRIEF_MAX_TOKENS с PLAN_MAX_TOKENS на подписке молча не значили ничего.
    Цена ошибки: план вернулся не планом, а «Claude's response exceeded the
    32000 output token maximum».
    """
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class P:
            returncode = 0
            pid = None

            async def communicate(self, input=None):
                return b'{"result": "ok", "usage": {}}', b""
        return P()

    monkeypatch.setattr("shutil.which", lambda *a: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(CliBackend().ask("вопрос", max_tokens=48000))
    assert seen["env"].get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "48000"
    # остальное окружение остаётся на месте: там ключи и PATH
    assert "PATH" in seen["env"] or "Path" in seen["env"]


def test_plan_ceiling_differs_by_backend():
    """У API и CLI пределы разные, и путать их нельзя."""
    from autopilot.config import cfg as c

    assert c.plan_max_tokens_cli > c.plan_max_tokens, (
        "на CLI план не помещался в 16k — потолок должен быть выше")
    assert c.plan_max_tokens_cli >= 32000, "32000 — то, во что упёрлись живьём"
