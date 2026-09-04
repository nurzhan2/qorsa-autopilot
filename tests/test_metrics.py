"""Отчёт о цикле: считает ли он то, что обещает, и не врёт ли пустотой.

Главное свойство — **пустая цифра не должна читаться как ноль**. Отчёт,
показывающий «0% работы впустую» на базе, где журнал ещё не заведён, хуже
отсутствующего: по нему примут решение.
"""
from __future__ import annotations

from conftest import make_project, make_tasks

from autopilot import metrics, readiness
from autopilot.db import Run, Session, Task, log_attempt, utcnow


async def add_run(task_id: int, *, kind="execute", backend="cli", cost=0.0,
                  seconds=0.0, estimated=False) -> None:
    async with Session() as s:
        s.add(Run(task_id=task_id, kind=kind, backend=backend, ok=True,
                  cost_usd=cost, seconds=seconds, cost_estimated=estimated,
                  started_at=utcnow(), finished_at=utcnow()))
        await s.commit()


async def close(task_id: int, last_error: str = "", verdict: dict | None = None) -> None:
    async with Session() as s:
        row = await s.get(Task, task_id)
        row.status = "done"
        row.last_error = last_error
        row.verdict = verdict or {}
        await s.commit()


# ---------- честность про пустоту ----------

async def test_empty_base_does_not_divide_by_zero(db):
    """Самый вероятный способ сломать отчёт — и самый обидный: он падает
    ровно тогда, когда его впервые запускают."""
    data = await metrics.overview(30)
    assert data["cost"]["median_usd"] == 0.0
    assert data["loop"]["wasted_pct"] == 0.0
    assert data["blind_spot"]["blind_time_pct"] == 0.0


async def test_gaps_say_what_is_not_recorded(db):
    gaps = await metrics.gaps()
    assert gaps["ledger_rows"] == 0
    assert any("журнал попыток пуст" in g for g in gaps["missing"])
    assert any("live_run" in g for g in gaps["missing"])


# ---------- деньги и подписка ----------

async def test_money_and_subscription_are_never_summed(db):
    """Складывать их — значит соврать в обе стороны сразу: на балансе
    не списано ни рубля, а суточный бюджет встанет."""
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    await add_run(t, backend="api", cost=3.0)
    await add_run(t, backend="cli", cost=10.0)

    s = await metrics.spend(30)
    assert s["money_usd"] == 3.0
    assert s["subscription_usd"] == 10.0
    assert "total_usd" not in s


async def test_unfinished_runs_are_counted_separately(db):
    """Пустой finished_at — вызов НЕ ВЕРНУЛСЯ. Он сжёг квоту и не оставил цены."""
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    async with Session() as s:
        s.add(Run(task_id=t, kind="execute", started_at=utcnow()))
        await s.commit()
    assert (await metrics.spend(30))["unfinished"] == 1


# ---------- слепая зона ----------

async def test_blind_spot_measures_time_not_money(db):
    """У оборванного вызова цена ноль, а время настоящее. Денежный потолок
    такой сессии не видит вовсе — её держит только потолок по числу сессий."""
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    await add_run(t, cost=5.0, seconds=600)                    # измерено
    await add_run(t, cost=0.0, seconds=1800, estimated=True)   # мимо счётчика

    b = await metrics.blind_spot(30)
    assert b["blind_runs"] == 1
    assert b["blind_time_pct"] == 75.0        # 30 мин из 40


# ---------- цена задачи ----------

async def test_cost_per_closed_task(db):
    p = await make_project()
    a, b, c = await make_tasks(p.id, 3)
    await add_run(a, cost=2.0)
    await add_run(b, cost=6.0)
    await add_run(c, cost=10.0)               # эта застрянет и будет ждать
    await close(a)
    await close(b)
    async with Session() as s:
        (await s.get(Task, c)).status = "escalated"
        await s.commit()

    out = await metrics.cost_per_task()
    assert out["closed_tasks"] == 2
    assert out["median_usd"] == 4.0
    assert out["stuck_usd"] == 10.0


async def test_queued_task_is_not_called_stuck(db):
    """Задача в очереди ничего не потратила впустую: её работа впереди.
    Смешать «ждёт человека» и «ещё не дошли руки» значило бы назвать
    нормальный ход цикла потерей."""
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    await add_run(t, cost=9.0)
    assert (await metrics.cost_per_task())["stuck_usd"] == 0.0


async def test_service_anchors_are_out_of_the_denominator(db):
    """`db.service_task` заводит якоря в полосе `chat` — к ним привязан расход
    брифа и плана. На живой базе их 38 против 23 настоящих закрытых: не
    отфильтруй — и доля закрытых завышается в 2.6 раза."""
    p = await make_project()
    await make_tasks(p.id, 2, lane="chat", status="done")
    real = await make_tasks(p.id, 1, lane="build")
    await add_run(real[0], cost=1.0)
    await close(real[0])

    out = await metrics.cost_per_task()
    assert out["closed_tasks"] == 1


# ---------- чем принимали ----------

async def test_acceptance_tells_proof_from_opinion(db):
    p = await make_project()
    proved, by_model, by_owner, old = await make_tasks(p.id, 4)
    await close(proved, verdict={"deterministic": 2, "assisted": 0})
    await close(by_model, verdict={"deterministic": 0, "assisted": 1})
    await close(by_owner, last_error="закрыто владельцем вручную; машина не приняла: …")
    await close(old)                                    # до миграции 14

    out = await metrics.acceptance()
    assert out == {"proved": 1, "by_model": 1, "by_owner": 1, "unknown": 1}


async def test_tainted_acceptance_is_not_called_proved(db):
    """Проверку правил проверяемый — такая задача не «доказана кодом»,
    даже если детерминированных проверок в ней было сколько угодно."""
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    await close(t, verdict={"deterministic": 3, "tainted": 1})
    assert (await metrics.acceptance()) == {"tainted": 1}


# ---------- форма цикла ----------

async def test_ledger_outcomes_are_reported(db):
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    await log_attempt(t, project_id=p.id, outcome="failed")
    await log_attempt(t, project_id=p.id, outcome="stagnation", counted=False)

    loop = await metrics.loop_shape()
    assert loop["ledger"] == {"failed": 1, "stagnation": 1}
    assert loop["ledger_since"]


async def test_no_invented_usefulness_score(db):
    """Взвешенного «балла пользы» здесь нет и не будет: коэффициенты берутся
    с потолка, а цифра выглядит измерением — и по ней немедленно захочется
    оптимизировать систему."""
    data = await metrics.overview(30)
    flat = str(data)
    assert "roi" not in flat.lower() and "score" not in flat.lower()


# ---------- готовность установки ----------

def test_readiness_names_the_fix_for_every_problem():
    """Пункт без «что делать» — это жалоба, а не диагностика."""
    for item in readiness.check():
        assert item.what and item.fix


def test_readiness_is_a_gate_not_a_score():
    """Балла 0–100 нет намеренно: он усредняет несравнимое — отсутствие
    браузера и отсутствие раздела в документации, — и по нему непонятно,
    что делать."""
    assert not hasattr(readiness, "score")
    assert isinstance(readiness.ready(), bool)


def test_missing_git_blocks_autonomy(monkeypatch):
    """Без git `_diff` возвращает пустую строку, судья получает пустой диф
    и честно судит пустоту — молча, без ошибки."""
    monkeypatch.setattr(readiness.shutil, "which", lambda name: None)
    items = readiness.check()
    git = next(i for i in items if i.key == "git")
    assert not git.ok and git.blocking
    assert not readiness.ready(items)


async def test_blind_spot_does_not_count_machine_sleep_as_work(db):
    """`time.monotonic()` на Windows идёт и во сне машины: в живой базе лежат
    «вызовы» на 6-7 часов, не сделавшие ни одного хода. Считать их работой —
    та же ошибка, что однажды дала фантомные $33 и остановила потолок
    на пустом месте.

    Слепая минута считается работой только до самого длинного ИЗМЕРЕННОГО
    вызова: дольше него настоящая работа не длилась ни разу.
    """
    p = await make_project()
    [t] = await make_tasks(p.id, 1)
    await add_run(t, cost=6.0, seconds=600)                      # 10 мин, измерено
    await add_run(t, cost=0.0, seconds=300, estimated=True)      # 5 мин, правдоподобно
    await add_run(t, cost=0.0, seconds=36000, estimated=True)    # 10 ч — сон машины

    b = await metrics.blind_spot(30)
    assert b["blind_minutes"] == 605.0            # честно: столько стенных часов
    assert b["plausible_minutes"] == 15.0         # 5 + обрезанные до 10 мин
    # Занижение считается по ПРАВДОПОДОБНЫМ минутам, а не по стенным часам:
    # по стенным вышло бы 605 мин × $0.6 = $363, то есть «занижено в 61 раз».
    # Такая цифра выглядит измерением и врёт больше, чем отсутствие цифры.
    assert b["understated_x"] == 2.5
