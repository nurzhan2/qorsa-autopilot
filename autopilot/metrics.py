"""Стало лучше или хуже — цифрами, а не ощущением.

Все пороги в этом проекте выведены из живых случаев, и это правильно. Но
проверить, что порог работает, было нечем: базу приходилось смотреть SQL-ом
руками, а половина вопросов вообще не имела ответа в схеме.

Что здесь считается и почему именно это:

* **сколько стоит ЗАКРЫТАЯ задача.** Не «сколько потрачено» — эта цифра
  ничего не говорит, пока не поделена на результат. На живой базе медиана
  $3.84, среднее $7.25, максимум $70.38: разброс в двадцать раз, и до этого
  модуля он был невидим;
* **сколько расхода ушло в задачи, которые ждут человека.** Пятая часть — это цена цикла,
  который буксует, и её надо видеть отдельно от цены работы;
* **какая доля времени вообще мимо счётчика.** Из ста сессий исполнителя
  двадцать две закрыты оценочной ценой в шесть центов при тысяче минут
  работы. То есть самый дорогой режим работы автопилота не учитывается
  ничем — знать это важнее любой другой цифры здесь;
* **сколько попыток впустую.** Прогоны, не приведшие к закрытию задачи;
* **чем принимались задачи.** Доказательством кода, мнением модели или
  рукой владельца — по `Task.verdict`, который пишется с миграции 14.

Чего здесь НЕТ и не будет:

* **никаких взвешенных «баллов пользы»** вроде `действия×10 − эскалации×5`
  из прототипа. Коэффициенты берутся с потолка, а цифра выглядит измерением —
  и по ней немедленно захочется оптимизировать систему. Этот проект уже
  записал правило: порог, взятый с потолка, врёт дважды;
* **никакой «доли успеха» как `(задачи − эскалации) / задачи`.** Эскалации
  считаются событиями, задачи — штуками; вычитая одно из другого, легко
  получить отрицательное число на совершенно обычном журнале. Доля считается
  по задачам и только по ним.

**Служебные якоря в знаменатель не идут.** `db.service_task` заводит по одной
задаче в полосе `chat` на проект и название — к ним привязан расход брифа
и плана. На живой базе их 38 против 23 настоящих закрытых: не отфильтруй —
и доля закрытых завышается в 2.6 раза.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter

from sqlalchemy import func, select

from .config import cfg
from .db import Run, Session, Task, TaskAttempt, utcnow

# Полосы настоящей работы. `chat` сюда не входит: там служебные якоря расхода
REAL_LANES = ("build", "verify")

CLOSED = ("done",)
STUCK = ("escalated", "needs_human", "awaiting_keys")


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


async def spend(days: int = 30) -> dict:
    """Расход: деньги и подписка РАЗДЕЛЬНО, и никогда не в сумме.

    Складывать их — значит соврать в обе стороны сразу: на балансе не списано
    ни рубля, а суточный бюджет встанет. Правило то же, что у `spent_today`.
    """
    since = utcnow() - dt.timedelta(days=days)
    async with Session() as s:
        rows = (await s.execute(
            select(Run.kind, Run.backend, Run.cost_usd, Run.seconds,
                   Run.cost_estimated, Run.finished_at)
            .where(Run.started_at >= since))).all()

    by_kind: dict[str, dict] = {}
    api = cli = 0.0
    unfinished = 0
    for kind, backend, cost, seconds, estimated, finished in rows:
        slot = by_kind.setdefault(kind, {"runs": 0, "usd": 0.0, "minutes": 0.0,
                                         "estimated": 0})
        slot["runs"] += 1
        slot["usd"] += float(cost or 0)
        slot["minutes"] += float(seconds or 0) / 60
        slot["estimated"] += 1 if estimated else 0
        if backend == "api":
            api += float(cost or 0)
        else:
            cli += float(cost or 0)
        if finished is None:
            unfinished += 1

    for slot in by_kind.values():
        slot["usd"] = round(slot["usd"], 2)
        slot["minutes"] = round(slot["minutes"], 1)
    return {"days": days, "runs": len(rows),
            "money_usd": round(api, 2), "subscription_usd": round(cli, 2),
            # Пустой finished_at — вызов НЕ ВЕРНУЛСЯ: либо идёт прямо сейчас,
            # либо процесс убили. Отдельной цифрой, потому что такие прогоны
            # сожгли квоту и не оставили цены
            "unfinished": unfinished,
            "by_kind": dict(sorted(by_kind.items(),
                                   key=lambda kv: -kv[1]["usd"]))}


async def blind_spot(days: int = 30) -> dict:
    """Какая доля работы исполнителя прошла МИМО счётчика расхода.

    Цену отдаёт CLI вместе с ответом; у вызова, срубленного таймаутом,
    ответа нет, и туда пишется `cost_usd=0, cost_estimated=True` — честно,
    но для потолка это ноль. На живой базе так закрыты 22 сессии из 100:
    шесть центов на тысячу минут работы.

    Это не ошибка учёта, а его известная граница, и она главный довод за
    третий потолок — по ЧИСЛУ СЕССИЙ (`db.sessions_today`), который не
    зависит ни от цены, ни от часов.
    """
    since = utcnow() - dt.timedelta(days=days)
    async with Session() as s:
        rows = (await s.execute(
            select(Run.cost_estimated, Run.cost_usd, Run.seconds)
            .where(Run.started_at >= since, Run.kind == "execute"))).all()

    def is_blind(est, cost):
        return bool(est) or not float(cost or 0)

    measured = [(float(c or 0), float(s or 0) / 60) for e, c, s in rows
                if not is_blind(e, c)]
    blind = [float(s or 0) / 60 for e, c, s in rows if is_blind(e, c)]
    total_min = sum(m for _, m in measured) + sum(blind)

    # ПОТОЛОК ПРАВДОПОДОБИЯ. Длительность слепого вызова — это стенные часы,
    # а `time.monotonic()` на Windows идёт и во сне машины: в базе лежат
    # «вызовы» на 420, 377 и 276 минут, которые не сделали ни одного хода.
    # Считать их работой — та же ошибка, что однажды дала фантомные $33
    # за прогон и остановила суточный потолок на пустом месте.
    #
    # Поэтому слепая минута считается работой только до самого длинного
    # ИЗМЕРЕННОГО вызова: дольше него настоящая работа не длилась ни разу.
    cap = max((m for _, m in measured), default=0.0)
    rate = (sum(c for c, _ in measured) / sum(m for _, m in measured)
            if measured and sum(m for _, m in measured) else 0.0)
    plausible = sum(min(m, cap) for m in blind) if cap else 0.0
    hidden_usd = plausible * rate
    known_usd = sum(c for c, _ in measured)

    return {"runs": len(rows),
            "blind_runs": len(blind),
            "minutes": round(total_min, 1),
            "blind_minutes": round(sum(blind), 1),
            # сколько из них правдоподобны как работа, а не как сон машины
            "plausible_minutes": round(plausible, 1),
            "blind_usd": round(sum(c for c, m in measured if False), 2),
            "blind_time_pct": _pct(sum(blind), total_min),
            # ВО СКОЛЬКО РАЗ занижен денежный счётчик. Оценка снизу-сверху,
            # а не замер: цену оборванного вызова не знает никто
            "hidden_usd": round(hidden_usd, 2),
            "understated_x": round((known_usd + hidden_usd) / known_usd, 1)
            if known_usd else 0.0}


async def cost_per_task() -> dict:
    """Во что обходится ЗАКРЫТАЯ задача и сколько ушло в ЗАСТРЯВШИЕ.

    «Застрявшая» — это задача, которая ждёт человека (`escalated`,
    `needs_human`, `awaiting_keys`), а не просто ещё не сделанная. Задача
    в очереди ничего не потратила впустую: её работа впереди. Смешать эти
    два состояния значило бы назвать нормальный ход цикла потерей.

    Считается по строкам `Run`, привязанным к задаче, а не по `Task.cost_usd`:
    на живых данных они расходятся до трёх раз, потому что не каждый вызов
    доходит до задачи.
    """
    async with Session() as s:
        per_task = dict((await s.execute(
            select(Run.task_id, func.sum(Run.cost_usd)).group_by(Run.task_id))).all())
        rows = (await s.execute(
            select(Task.id, Task.status).where(Task.lane.in_(REAL_LANES)))).all()

    closed, stuck, other = [], 0.0, 0.0
    for task_id, status in rows:
        cost = float(per_task.get(task_id) or 0)
        if status in CLOSED:
            closed.append(cost)
        elif status in STUCK:
            stuck += cost
        else:
            other += cost

    paid = [c for c in closed if c > 0]
    spent_total = sum(closed) + stuck + other
    return {"closed_tasks": len(closed),
            # Только те, у кого расход есть. Задача, закрытая рукой без единого
            # прогона, стоила ноль — включать её в медиану значит занижать
            "priced": len(paid),
            "median_usd": round(statistics.median(paid), 2) if paid else 0.0,
            "mean_usd": round(statistics.fmean(paid), 2) if paid else 0.0,
            "max_usd": round(max(paid), 2) if paid else 0.0,
            "stuck_usd": round(stuck, 2),
            "stuck_pct": _pct(stuck, spent_total)}


async def loop_shape() -> dict:
    """Форма цикла: сколько задача ходит по кругу и сколько ходов впустую.

    Две цифры считаются по РАЗНЫМ источникам, и это указано явно:

    * по `Run` — сколько сессий пришлось на задачу. Есть за всю историю базы,
      но не отличает работу от повтора одного и того же;
    * по журналу попыток (`task_attempts`) — что именно происходило. Есть
      только с миграции 14, поэтому у задач старше журнал пуст. Пустой журнал
      здесь означает «не записано», а не «попыток не было».
    """
    async with Session() as s:
        runs = dict((await s.execute(
            select(Run.task_id, func.count()).where(Run.kind == "execute")
            .group_by(Run.task_id))).all())
        rows = (await s.execute(
            select(Task.id, Task.status).where(Task.lane.in_(REAL_LANES)))).all()
        ledger = (await s.execute(
            select(TaskAttempt.outcome, func.count())
            .group_by(TaskAttempt.outcome))).all()
        first = (await s.execute(select(func.min(TaskAttempt.at)))).scalar()

    hist = Counter(int(runs.get(task_id) or 0) for task_id, _ in rows)
    total_runs = sum(runs.get(task_id, 0) for task_id, _ in rows)
    closed_runs = sum(runs.get(task_id, 0) for task_id, status in rows
                      if status in CLOSED)
    statuses = Counter(status for _, status in rows)
    return {"tasks": len(rows),
            "sessions": total_runs,
            # Сессии, не приведшие к закрытой задаче. Не «плохие»: часть из них
            # — законные повторы. Но доля впустую — та цифра, которую и надо
            # уменьшать, а до этого модуля её никто не считал
            "wasted_sessions": total_runs - closed_runs,
            "wasted_pct": _pct(total_runs - closed_runs, total_runs),
            "sessions_hist": dict(sorted(hist.items())),
            "statuses": dict(statuses.most_common()),
            "ledger": dict(ledger),
            "ledger_since": first.isoformat(timespec="minutes") if first else None}


async def acceptance() -> dict:
    """Чем принимались задачи: доказательством кода или чем-то ещё.

    Читается `Task.verdict`, который пишется с миграции 14. У задач старше
    его нет — и это `unknown`, а не «принято плохо». Пустая цифра не должна
    читаться как ноль: ровно за этим здесь отдельный счётчик.
    """
    async with Session() as s:
        rows = (await s.execute(
            select(Task.status, Task.verdict, Task.last_error)
            .where(Task.lane.in_(REAL_LANES)))).all()

    out = Counter()
    for status, verdict, last_error in rows:
        if status not in CLOSED:
            continue
        # Закрытие рукой видно по следу, который оставляет `manual.confirm`
        if "закрыто владельцем вручную" in str(last_error or ""):
            out["by_owner"] += 1
            continue
        if not isinstance(verdict, dict) or not verdict:
            out["unknown"] += 1
            continue
        if int(verdict.get("tainted") or 0):
            out["tainted"] += 1
        elif int(verdict.get("deterministic") or 0) > 0:
            out["proved"] += 1
        elif int(verdict.get("assisted") or 0) > 0:
            out["by_model"] += 1
        else:
            out["unknown"] += 1
    return dict(out)


async def gaps() -> dict:
    """Чего в базе НЕТ — отдельным разделом, а не умолчанием.

    Пустая строка отчёта неотличима от нуля, и это самый простой способ
    соврать цифрами. То же соображение, из-за которого в брифе есть
    `unreadable[]`, а в вердикте — `deferred`.
    """
    async with Session() as s:
        ledger_rows = (await s.execute(
            select(func.count()).select_from(TaskAttempt))).scalar() or 0
        verdicts = (await s.execute(
            select(func.count()).select_from(Task)
            .where(Task.verdict != {}, Task.lane.in_(REAL_LANES)))).scalar() or 0

    out = []
    if not ledger_rows:
        out.append("журнал попыток пуст: он ведётся с миграции 14, а всё, что "
                   "было раньше, восстановить нечем — у старых задач сохранился "
                   "только последний набор дефектов")
    if not verdicts:
        out.append("состав приёмки (Task.verdict) не записан ни у одной задачи: "
                   "он тоже с миграции 14")
    out.append("расход прогонов, оборванных таймаутом, — ноль по цене при "
               "ненулевом времени: см. blind_spot()")
    out.append("scripts/live_run.py зовёт исполнителя МИМО планировщика, "
               "его прогоны попадают в Run, но не в журнал попыток")
    return {"missing": out, "ledger_rows": ledger_rows, "verdicts": verdicts}


async def overview(days: int = 30) -> dict:
    """Всё сразу — для отчёта и для дашборда."""
    return {"spend": await spend(days), "blind_spot": await blind_spot(days),
            "cost": await cost_per_task(), "loop": await loop_shape(),
            "acceptance": await acceptance(), "gaps": await gaps(),
            "budgets": {"money": cfg.daily_budget_usd,
                        "subscription": cfg.daily_cli_budget_usd,
                        "sessions": cfg.daily_cc_sessions}}
