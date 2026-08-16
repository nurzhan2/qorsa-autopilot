"""Фаза 9: API и CLI как взаимозаменяемые бэкенды, упор в квоту подписки.

Главное здесь — упор в квоту НЕ ЕСТЬ провал работы. Раньше любой сбой вызова
означал неудачную попытку, и четыре упора подряд увели бы живую задачу
в эскалацию, а бриф обнулили бы.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeCommunicator, make_project, make_tasks

from autopilot import limits, llm
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
