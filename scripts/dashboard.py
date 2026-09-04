"""Локальный дашборд: что происходит, что от меня ждут и куда вставить ТЗ.

    python scripts/dashboard.py
    открыть http://127.0.0.1:8787

Одна страница без сборки, без npm и без фронтенд-фреймворков: FastAPI отдаёт
HTML и JSON, страница раз в три секунды спрашивает состояние обычным fetch.

**Слушает только localhost.** Здесь видны имена клиентов, суммы и то, чего мы
ждём от людей; в локальной сети этому делать нечего.

**Значения секретов отсюда не отдаются никогда.** Окно секретов умеет их
класть в хранилище и показывать ИМЕНА — но не значения: страница, которая
печатает пароль, тем самым кладёт его в скроллбек браузера и в его историю.

Порядок блоков не косметика: **сначала то, что ждёт человека**. Задача
в `needs_human` или `escalated` стоит на месте до решения владельца, и если
она утонет в общем списке, автопилот тихо встанет.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import func, select                                    # noqa: E402

from autopilot import accounts as accounts_cfg                        # noqa: E402
from autopilot import (autonomy, crew, curate, intake, limits, manual,  # noqa: E402
                       progress, projectdir, readiness, roles, toolchain)
from autopilot.config import cfg                                      # noqa: E402
from autopilot.db import (Account, AccessItem, ChatMessage, Message,  # noqa: E402
                          Project, ProjectChat, Run, Session, Task,
                          cli_spent_today, consumed_today, init_db,
                          sessions_today, spent_today, sync_accounts, utcnow)
from autopilot.vault import resolve_secret, vault                     # noqa: E402
from autopilot.verifier import Verifier                               # noqa: E402

log = logging.getLogger("dashboard")

# Пауза — файл, а не поле в базе: её должен уметь снять человек, даже если
# процесс автопилота лежит. Путь один на всех (progress.pause_path): пока
# он был объявлен здесь отдельно, планировщик его не читал вовсе — кнопка
# писала файл, страница показывала «на паузе», а работа шла дальше
PAUSE_FILE = progress.pause_path()

STATUS_RU = {
    "new": "новый", "briefing": "собираем ТЗ", "active": "в работе",
    "review": "на приёмке", "done": "сдан", "blocked": "заблокирован",
    "blocked_access": "ждём доступы",
}
TASK_RU = {
    "pending": "ждёт", "ready": "в очереди", "running": "выполняется",
    "done": "готово", "escalated": "эскалация", "needs_human": "нужен человек",
    autonomy.AWAITING_KEYS: "ждёт ключа",
}
# Вид вызова модели по-русски. В таблице расхода печатался сырой `kind`,
# и «execute» рядом с «сбор ТЗ» читалось как чужая строка.
RUN_RU = {
    "execute": "сессия агента", "judge": "судья", "brief": "сбор ТЗ",
    "plan": "планирование", "prompt": "задание агенту",
    "diagnose": "разбор застревания",
}

# Статусы, в которых задача ЖДЁТ ЧЕЛОВЕКА: для них считаем возраст ожидания.
# Задача в очереди не ждёт — её работа впереди, и «стоит 40 часов» про неё
# было бы неправдой.
WAITING_STATUSES = ("needs_human", "escalated", autonomy.AWAITING_KEYS)

ACCESS_RU = {
    "needed": "нужен", "requested": "запрошен", "received": "получен",
    "verified": "есть", "failed": "не подошёл",
    autonomy.PLACEHOLDER: "заглушка",
}

SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# ---------- фоновые прогоны ----------
#
# Сбор ТЗ и планирование идут минутами, а то и десятками минут. Держать на
# них HTTP-запрос нельзя: браузер отвалится по таймауту, а прогон продолжится
# — и человек не будет знать, чем он кончился. Поэтому запуск возвращается
# сразу, а состояние видно в общем снимке.
JOBS: dict[str, dict] = {}

# Виды прогонов. Список нужен, чтобы снимок состояния не забыл ни один:
# `refresh` однажды уже не попал в карточку, и нажатие кнопки выглядело как
# «ничего не произошло», хотя цепочка шла
JOB_KINDS = ("brief", "plan", "refresh")

JOB_RU = {"brief": "сбор ТЗ", "plan": "планирование",
          "refresh": "пересборка ТЗ и плана"}


def _job_key(kind: str, project_id: int) -> str:
    return f"{kind}:{project_id}"


async def _run_job(kind: str, project_id: int, coro_factory) -> None:
    key = _job_key(kind, project_id)
    started = utcnow()
    JOBS[key] = {"kind": kind, "kind_ru": JOB_RU.get(kind, kind),
                 "project_id": project_id, "state": "running",
                 "started": started.isoformat()[:19], "message": "",
                 "seconds": 0.0}
    try:
        message = await coro_factory()
        JOBS[key].update(state="ok", message=message or "готово")
    except Exception as e:                      # noqa: BLE001 — показать человеку
        log.exception("прогон %s по проекту %s упал", kind, project_id)
        JOBS[key].update(state="failed", message=f"{type(e).__name__}: {e}"[:300])
    finally:
        JOBS[key]["finished"] = utcnow().isoformat()[:19]
        JOBS[key]["seconds"] = round((utcnow() - started).total_seconds(), 1)


def _job_view(row: dict | None) -> dict | None:
    """Прогон для страницы: у идущего считаем, сколько он уже идёт.

    Без этого «идёт» выглядит одинаково и через секунду, и через двадцать
    минут — то есть не отвечает на вопрос, ради которого его смотрят.
    """
    if not row:
        return None
    out = dict(row)
    if out.get("state") == "running":
        started = dt.datetime.fromisoformat(out["started"]).replace(
            tzinfo=dt.timezone.utc)
        out["seconds"] = round((utcnow() - started).total_seconds(), 1)
    return out


def _start_job(kind: str, project_id: int, coro_factory) -> dict:
    key = _job_key(kind, project_id)
    running = JOBS.get(key)
    if running and running.get("state") == "running":
        return {"ok": False, "message": "этот прогон уже идёт"}
    asyncio.create_task(_run_job(kind, project_id, coro_factory))
    return {"ok": True, "message": "запущено, следи за состоянием на карточке"}


async def _collect_brief(project_id: int) -> str:
    from autopilot.brief import Brief, pending_questions

    async with Session() as s:
        project = await s.get(Project, project_id)
    if project is None:
        raise ValueError("проекта нет")
    data = await Brief(communicator=None).build(project)
    if data is None:
        return "новых сообщений нет — модель не дёргалась"
    questions = await pending_questions(project)
    return (f"ТЗ обновлено: пунктов {len(data.get('deliverables') or [])}, "
            f"уверенность {float(data.get('confidence') or 0):.2f}, "
            f"вопросов к клиенту {len(questions)}")


async def _collect_plan(project_id: int) -> str:
    from autopilot.planner import Planner

    async with Session() as s:
        project = await s.get(Project, project_id)
    if project is None:
        raise ValueError("проекта нет")
    result = await Planner(communicator=None).plan(project)
    if result is None:
        return "план не собран — смотри лог, проект мог уйти в blocked"
    stats = result["stats"]
    return (f"план собран: задач {stats['tasks']}, автономность "
            f"{stats['auto_ratio'] * 100:.0f}% по числу и "
            f"{stats['auto_ratio_time'] * 100:.0f}% по времени")


# ---------- секреты ----------

async def secret_state() -> dict:
    """Что в хранилище есть и чего не хватает. ТОЛЬКО ИМЕНА.

    «Не хватает» собирается из трёх мест, и все три — реальные причины
    остановки: без токена компания не поднимает бота, без доступа клиента
    задача не выполнится, без ключа не судить скриншоты.
    """
    known: list[str] = []
    if vault.enabled:
        try:
            known = vault.names()
        except RuntimeError as e:
            return {"enabled": True, "broken": str(e), "known": [], "missing": []}

    missing: list[dict] = []
    seen: set[str] = set()

    def want(name: str, why: str) -> None:
        name = (name or "").strip()
        if not name or name in seen:
            return
        seen.add(name)
        value, _where = resolve_secret(name)
        if not value:
            missing.append({"name": name, "why": why})

    for acc in accounts_cfg.load():
        if not acc.active:
            continue
        if acc.bot_token_ref:
            want(acc.bot_token_ref,
                 f"без него бот компании «{acc.title}» не поднимается")
        if getattr(acc, "max_token_ref", ""):
            want(acc.max_token_ref, f"мессенджер MAX для «{acc.title}»")

    want("ANTHROPIC_API_KEY",
         "нужен только на LLM_BACKEND=api и для судьи по скриншотам; "
         "на подписке работа идёт без него")

    async with Session() as s:
        items = (await s.execute(
            select(AccessItem, Project.title)
            .join(Project, Project.id == AccessItem.project_id)
            .where(AccessItem.secret_ref.isnot(None),
                   AccessItem.stale.is_(False)))).all()
    for item, project_title in items:
        for name in _refs(item.secret_ref):
            want(name, f"доступ «{item.name}» проекта «{project_title}»")

    return {"enabled": vault.enabled, "broken": "",
            "known": known, "missing": missing}


def _refs(text: str | None) -> list[str]:
    from autopilot.vault import refs_in
    return refs_in(text or "")


# ---------- снимок состояния ----------

async def collect() -> dict:
    """Всё состояние одним снимком: страница ходит сюда раз в три секунды."""
    async with Session() as s:
        accounts = {a.id: a for a in (await s.execute(select(Account))).scalars()}
        projects = (await s.execute(
            select(Project).order_by(Project.id))).scalars().all()
        tasks = (await s.execute(
            select(Task).where(Task.lane.in_(("build", "verify")))
            .order_by(Task.project_id, Task.order_idx, Task.id))).scalars().all()
        runs = (await s.execute(
            select(Run).order_by(Run.id.desc()).limit(25))).scalars().all()
        # что шевелилось последним — лента «как идёт процесс».
        # По updated_at, а не по id: id говорит, когда задачу ЗАВЕЛИ
        recent = (await s.execute(
            select(Task).where(Task.lane.in_(("build", "verify")),
                               Task.orphaned.is_(False))
            .order_by(Task.updated_at.desc()).limit(12))).scalars().all()
        # `stale` берём тоже: работу они не держат (см. Scheduler), но
        # показать их надо — пункт мог выпасть из ТЗ переформулировкой,
        # а доступ по нему всё равно нужен, и вставить его должно быть куда
        access = (await s.execute(
            select(AccessItem).where(AccessItem.status != "verified")
            .order_by(AccessItem.stale, AccessItem.id))).scalars().all()
        # одним запросом, а не по проекту: страница обновляется каждые
        # три секунды, и N+1 здесь превращается в постоянную нагрузку на базу,
        # в которую в это же время пишет планировщик
        msg_counts = dict((await s.execute(
            select(ChatMessage.project_id, func.count())
            .where(ChatMessage.project_id.isnot(None))
            .group_by(ChatMessage.project_id))).all())
        # Сколько сессий съела задача и во что обошлась — ОДНИМ группированным
        # запросом, по образцу msg_counts выше. Задача 531 сожгла 24 сессии
        # и $70, а на карточке было видно только «попыток 4»: attempts
        # не считает ни повторов из live_run.py, ни ручных прогонов.
        # `Task.cost_usd` для этого не годится — на живых данных он расходится
        # с суммой по Run до трёх раз
        task_runs = dict((await s.execute(
            select(Run.task_id, func.count()).where(Run.kind == "execute")
            .group_by(Run.task_id))).all())
        task_cost = dict((await s.execute(
            select(Run.task_id, func.sum(Run.cost_usd))
            .group_by(Run.task_id))).all())

    by_project: dict[int, list[Task]] = {}
    for t in tasks:
        by_project.setdefault(t.project_id, []).append(t)
    # Чеглист доступов — структурой, а не строкой: у каждого пункта своё окно
    # для значения на карточке заказа. Раньше это была строка «ждём от клиента»,
    # по которой человек шёл в CLI и вставлял ключ руками
    waiting_access: dict[int, list[dict]] = {}
    for a in access:
        waiting_access.setdefault(a.project_id, []).append({
            "id": a.id, "name": a.name, "kind": a.kind, "status": a.status,
            "status_ru": ACCESS_RU.get(a.status, a.status),
            "secret": autonomy.suggest_secret_name(a.project_id, a),
            "placeholder": a.status == autonomy.PLACEHOLDER,
            "stale": bool(a.stale),
            "note": a.note or "",
        })

    now = utcnow()

    def task_row(t: Task) -> dict:
        # «Ждёт окружение» ли задача — считаем только для того, что реально
        # стоит и ждёт человека: на остальных это просто лишняя работа.
        env_hints = []
        if t.status in ("needs_human", "escalated"):
            criteria_text = " ".join(
                str(c) for c in (t.acceptance or []) if isinstance(c, dict))
            env_hints = toolchain.tool_hints_in_text(
                " ".join(t.defects or []), criteria_text)
        return {
            "id": t.id, "title": t.title, "status": t.status,
            "status_ru": TASK_RU.get(t.status, t.status),
            "verify_class": t.verify_class, "executor": t.executor,
            "attempts": t.attempts, "orphaned": bool(t.orphaned),
            "defects": list(t.defects or []),
            "observations": list(t.observations or []),
            "waits_confirmation": manual.waits_for_confirmation(t),
            "project_id": t.project_id,
            "env_hints": env_hints,
            # Разбор застрявшей задачи: почему цикл не сходится. Без него
            # человек видит только «попыток 4» и тот же дефект четырежды
            "diagnosis": (crew.diagnosis_note(t.diagnosis)
                          if isinstance(t.diagnosis, dict) and t.diagnosis.get("verdict")
                          else ""),
            # Сколько эта задача уже стоила. `attempts` этого не показывает:
            # он не считает ни прогонов из live_run.py, ни ручных заходов
            "runs": int(task_runs.get(t.id) or 0),
            "spent": round(float(task_cost.get(t.id) or 0), 2),
            # СКОЛЬКО ЧАСОВ ЖДЁТ. Задача, стоящая третьи сутки, выглядела
            # ровно как поставленная десять минут назад — а это разные
            # состояния, и второе требует действия
            "waiting_hours": (round((now - t.updated_at).total_seconds() / 3600, 1)
                              if t.status in WAITING_STATUSES else None),
            # Цикл стоит на месте: дефект повторяется. Пометка, а не приговор —
            # решение всё равно за человеком
            "stuck": "один и тот же дефект" in (t.last_error or ""),
        }

    rows = []
    for p in projects:
        mine = by_project.get(p.id, [])
        live = [t for t in mine if not t.orphaned]
        done = [t for t in live if t.status == "done"]
        brief = (p.brief or {}).get("brief") if isinstance(p.brief, dict) else None
        folder = projectdir.find(p.id)
        # Почему заказ стоит. Гейтов четыре, и закрытых обычно не один —
        # сняв первый, человек справедливо ждал бы, что поедет
        open_tasks = [t for t in live if t.status not in ("done", "escalated")]
        agent_tasks = [t for t in open_tasks
                       if t.executor == "claude_code" and t.verify_class != "human"]
        waiting_now = [a for a in waiting_access.get(p.id, [])
                       if not a["stale"] and not a["placeholder"]]
        why_stuck = progress.blockers(
            p, access_waiting=len(waiting_now),
            blocking_questions=sum(1 for q in ((brief or {}).get("open_questions") or [])
                                   if q.get("blocking")),
            open_tasks=len(open_tasks), agent_tasks=len(agent_tasks))
        rows.append({
            "id": p.id,
            "company": (accounts.get(p.account_id).title
                        if accounts.get(p.account_id) else "—"),
            "client": p.client, "title": p.title,
            "status": p.status, "status_ru": STATUS_RU.get(p.status, p.status),
            "progress": f"{len(done)}/{len(live)}" if live else "—",
            "percent": round(len(done) / len(live) * 100) if live else 0,
            "cost": round(p.cost_usd or 0, 2),
            "access": waiting_access.get(p.id, []),
            "autonomous": bool(p.autonomous_mode),
            "awaiting_keys": sum(1 for t in live
                                 if t.status == autonomy.AWAITING_KEYS),
            "brief_ready": bool(p.brief_ready),
            "ready_for_work": bool(p.ready_for_work),
            "autonomy": round((p.autonomy_ratio or 0) * 100),
            "autonomy_time": round((p.autonomy_ratio_time or 0) * 100),
            "deliverables": len((brief or {}).get("deliverables") or []),
            "questions": [q.get("text", "") for q in
                          ((brief or {}).get("open_questions") or [])[:5]],
            "messages": int(msg_counts.get(p.id, 0)),
            "folder": str(folder) if folder else "",
            "planned": bool(p.planned_at),
            "requirements": curate.listing(p),
            "orphans": sum(1 for t in mine if t.orphaned),
            "why_stuck": why_stuck,
            "running_now": [t.title for t in live if t.status == "running"],
            "last_action": p.last_action or "",
            # Все виды прогонов, включая цепочку `refresh`: раньше её тут
            # не было, и нажатие «Пересобрать ТЗ и план» не оставляло на
            # карточке никакого следа — человек не понимал, пошло или нет
            "jobs": {kind: _job_view(JOBS.get(_job_key(kind, p.id)))
                     for kind in JOB_KINDS
                     if JOBS.get(_job_key(kind, p.id))},
            "tasks": [task_row(t) for t in live],
        })

    stuck = [task_row(t) for t in tasks
             if not t.orphaned and t.status in ("needs_human", "escalated")]
    attention_env = [t for t in stuck if t["env_hints"]]
    attention_review = [t for t in stuck if not t["env_hints"]]

    tools_needed: dict[str, dict] = {}
    for t in attention_env:
        for h in t["env_hints"]:
            row = tools_needed.setdefault(h["tool"], {**h, "tasks": []})
            row["tasks"].append(t["title"])
    install_list = sorted(tools_needed.values(), key=lambda r: r["name"])

    both = await consumed_today()
    beat = progress.read_heartbeat()
    money, subscription = await spent_today(), await cli_spent_today()
    # Третий потолок — по ЧИСЛУ СЕССИЙ. Единственный, который не врёт
    # при оборванных вызовах: у них цена ноль, а квота сожжена
    sessions = await sessions_today()

    # Задачи, которые ПРЯМО СЕЙЧАС в работе, и сколько уже идут.
    # `updated_at` для running — это момент захвата задачи планировщиком
    now = utcnow()
    running = [{
        "id": t.id, "title": t.title, "project_id": t.project_id, "lane": t.lane,
        "minutes": round((now - t.updated_at).total_seconds() / 60, 1),
    } for t in tasks if t.status == "running"]

    # Незакрытые строки расхода — вызов модели, который идёт прямо сейчас
    # (или процесс убили на середине, см. db.open_run)
    open_runs = [{
        "id": r.id, "task_id": r.task_id, "kind": r.kind,
        "kind_ru": RUN_RU.get(r.kind, r.kind),
        "minutes": round((now - r.started_at).total_seconds() / 60, 1),
    } for r in runs if r.finished_at is None]

    return {
        "projects": rows,
        "beat": beat,
        "running": running,
        "open_runs": open_runs,
        "feed": [{
            "id": t.id, "title": t.title, "project_id": t.project_id,
            "status": t.status, "status_ru": TASK_RU.get(t.status, t.status),
            "lane": t.lane, "at": str(t.updated_at)[:19],
            "minutes": round((now - t.updated_at).total_seconds() / 60, 1),
        } for t in recent],
        "global_blockers": progress.global_blockers(
            paused=PAUSE_FILE.exists(), alive=bool(beat.get("alive")),
            over_money=money >= cfg.daily_budget_usd,
            over_cli=subscription >= cfg.daily_cli_budget_usd,
            limited_until=limits.state.seconds_left(),
            # Тихие часы считает планировщик, а не мы: второе мнение о том же
            # самом однажды разойдётся с первым. Автопилот не запущен — про
            # тихие часы говорить нечего, о нём и так сказано отдельно
            build_allowed=bool(beat.get("build_allowed", True))
            if beat.get("alive") else True,
            over_sessions=sessions >= cfg.daily_cc_sessions,
            # Чего не хватает установке. Считается ЗДЕСЬ, а не берётся
            # из пульса: это состояние машины, а не цикла, и знать о нём
            # надо даже когда автопилот не запущен — тем более тогда
            install=readiness.problems()),
        "attention": {"environment": attention_env, "review": attention_review},
        "install_list": install_list,
        "companies": [{"code": a.code, "title": a.title, "transport": a.transport}
                      for a in accounts.values() if a.active],
        "secrets": await secret_state(),
        "runs": [{
            "id": r.id, "task_id": r.task_id, "kind": r.kind,
            "kind_ru": RUN_RU.get(r.kind, r.kind), "ok": bool(r.ok),
            "backend": r.backend, "cost": round(r.cost_usd or 0, 3),
            "estimated": bool(r.cost_estimated),
            "minutes": round((r.seconds or 0) / 60, 1),
            "started": str(r.started_at)[:19],
            "finished": str(r.finished_at)[:19] if r.finished_at else None,
        } for r in runs],
        "spend": {
            "api": round(both["api_usd"], 2),
            "api_limit": cfg.daily_budget_usd,
            "cli": round(both["cli_usd_est"], 2),
            "cli_limit": cfg.daily_cli_budget_usd,
            "api_calls": int(both["api_calls"]), "cli_calls": int(both["cli_calls"]),
            "cli_minutes": round(both["cli_seconds"] / 60, 1),
        },
        "paused": PAUSE_FILE.exists(),
        "max_attempts": cfg.max_attempts,
        "now": str(utcnow())[:19],
    }


def build_app():
    import contextlib

    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Схему поднимают миграции: дашборд может оказаться первым, кто
        # открыл базу после обновления кода
        await init_db()
        # Компании из accounts.toml — в таблицу: без строки Account проект
        # из окна завести нельзя (account_id NOT NULL), а дашборд вполне
        # может быть запущен раньше главного процесса
        try:
            await sync_accounts(accounts_cfg.active())
        except Exception:
            log.exception("компании не синхронизированы")
        yield

    app = FastAPI(title="Qorsa Autopilot", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    async def state():
        return JSONResponse(await collect())

    # ---------- задачи ----------

    @app.post("/api/task/{task_id}/done")
    async def mark_done(task_id: int):
        """«Сделал, проверь» — те же критерии, что и для агента."""
        ok, defects = await manual.submit(task_id, Verifier())
        return {"ok": ok, "defects": defects}

    @app.post("/api/task/{task_id}/retry")
    async def retry_task(task_id: int):
        """Вернуть задачу агенту: «сделай ещё раз».

        Нужно там, где работа НЕ сделана, а задача уже снята с работы:
        исчерпаны попытки или приёмке нечем было её принять. Кнопка
        «Я сделал, проверь» тут не подходит — она проверяет то, чего нет,
        и снова проваливается.

        Счётчик попыток обнуляется намеренно: владелец решил дать задаче
        новый заход, и старый лимит его решение отменять не должен. Но
        решение записывается в `last_error` — по базе должно быть видно,
        что счётчик сбросила рука, а не сама система.
        """
        async with Session() as s:
            t = await s.get(Task, task_id)
            if t is None:
                raise HTTPException(404, "задачи нет")
            if t.status not in ("escalated", "needs_human", autonomy.AWAITING_KEYS):
                return {"ok": False,
                        "message": f"задача в статусе «{TASK_RU.get(t.status, t.status)}» "
                                   f"— возвращать в очередь нечего"}
            was, attempts = t.status, t.attempts
            t.status = "ready"
            t.lane = "build"
            t.defects = []
            t.attempts = 0
            t.last_error = (f"возвращена в работу владельцем из «{was}»; "
                            f"счётчик попыток сброшен (было {attempts})")
            t.updated_at = utcnow()
            await s.commit()
        log.warning("задача %s возвращена в очередь вручную (была %s, попыток %s)",
                    task_id, was, attempts)
        return {"ok": True, "message": "Задача снова в очереди — агент возьмёт её "
                                       "на ближайшем тике."}

    @app.post("/api/task/{task_id}/confirm")
    async def confirm(task_id: int):
        """«Принимаю под свою ответственность» — только для needs_human."""
        ok, message = await manual.confirm(task_id)
        return {"ok": ok, "message": message}

    # ---------- приём заказа ----------

    @app.post("/api/intake/preview")
    async def intake_preview(payload: dict = Body(...)):
        """Что мы поняли из вставленного — ДО того, как что-то записано.

        Ошибка в ролях портит ТЗ целиком: мои собственные предложения уедут
        в требования клиента. Поэтому раскладку показываем и просим
        подтвердить, а не расставляем молча.
        """
        parsed = intake.parse(str(payload.get("raw") or ""))
        account = accounts_cfg.by_code(str(payload.get("company") or "")) or None
        return {
            "kind": parsed.kind,
            "count": len(parsed.lines),
            "speakers": parsed.speakers,
            "roles": intake.guess_roles(parsed, account),
            "notes": parsed.notes,
            "sample": [{"speaker": ln.speaker, "text": ln.text[:200]}
                       for ln in parsed.lines[:6]],
        }

    @app.post("/api/intake/create")
    async def intake_create(payload: dict = Body(...)):
        raw = str(payload.get("raw") or "")
        parsed = intake.parse(raw)
        if not parsed.ok:
            raise HTTPException(400, "в тексте не нашлось ни одной реплики")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "у заказа должно быть название")
        try:
            project = await intake.create_project(
                account_code=str(payload.get("company") or ""),
                client=str(payload.get("client") or ""),
                title=title,
                deadline=_parse_deadline(payload.get("deadline")),
                priority=int(payload.get("priority") or 2),
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        stats = await intake.apply(project.id, parsed,
                                   _roles_from(payload, parsed),
                                   source="дашборд: новый заказ")
        return {"ok": True, "project_id": project.id, "stats": stats}

    @app.post("/api/intake/append")
    async def intake_append(payload: dict = Body(...)):
        """Дозагрузка переписки в уже заведённый проект."""
        raw = str(payload.get("raw") or "")
        parsed = intake.parse(raw)
        if not parsed.ok:
            raise HTTPException(400, "в тексте не нашлось ни одной реплики")
        project_id = int(payload.get("project_id") or 0)
        try:
            stats = await intake.apply(project_id, parsed,
                                       _roles_from(payload, parsed),
                                       source="дашборд: дозагрузка")
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        return {"ok": True, "project_id": project_id, "stats": stats}

    # ---------- прогоны ----------

    @app.post("/api/project/{project_id}/brief")
    async def run_brief(project_id: int):
        return _start_job("brief", project_id, lambda: _collect_brief(project_id))

    @app.post("/api/project/{project_id}/plan")
    async def run_plan(project_id: int):
        return _start_job("plan", project_id, lambda: _collect_plan(project_id))

    # ---------- правки ТЗ ----------
    #
    # План правится ЧЕРЕЗ ТЗ, а не мимо него. Задача, дописанная в план руками,
    # не найдёт своего пункта и на следующем прогоне планировщика уедет
    # в `orphaned` — правка молча откатится. Поэтому дописываем требование,
    # а план пересобираем.

    @app.post("/api/project/{project_id}/requirement")
    async def add_requirement(project_id: int, payload: dict = Body(...)):
        try:
            return {"ok": True, **await curate.add(
                project_id, str(payload.get("text") or ""),
                field=str(payload.get("field") or "deliverables"),
                priority=str(payload.get("priority") or "must"))}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/project/{project_id}/requirement/drop")
    async def drop_requirement(project_id: int, payload: dict = Body(...)):
        try:
            return {"ok": True, **await curate.drop(
                project_id, str(payload.get("text") or ""),
                field=str(payload.get("field") or "deliverables"))}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/project/{project_id}/requirement/restore")
    async def restore_requirement(project_id: int, payload: dict = Body(...)):
        try:
            return {"ok": True, **await curate.restore(
                project_id, str(payload.get("text") or ""))}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/project/{project_id}/question")
    async def answer_question(project_id: int, payload: dict = Body(...)):
        """Владелец ответил на блокирующий вопрос ТЗ сам.

        Ответ уходит и в переписку (иначе следующий пересбор спросит то же
        самое), и в ТЗ ручным пунктом с `origin="owner"`. Выдавать решение
        владельца за слова клиента нельзя — на этом держится evidence.
        """
        try:
            return {"ok": True, **await curate.answer_question(
                project_id, str(payload.get("question") or ""),
                str(payload.get("answer") or ""))}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/project/{project_id}/refresh")
    async def refresh(project_id: int):
        """Пересобрать ТЗ и следом перепланировать — одной кнопкой.

        Именно в этом порядке и именно цепочкой: план выводится из ТЗ, и
        планировать по несобранному значит планировать по старому. Раньше
        это были две кнопки, и между ними легко было забыть вторую.
        """
        async def chain() -> str:
            first = await _collect_brief(project_id)
            second = await _collect_plan(project_id)
            return f"{first}; {second}"

        return _start_job("refresh", project_id, chain)

    @app.post("/api/project/{project_id}/prune")
    async def prune(project_id: int):
        """Убрать сирот — задачи, требование которых исчезло из ТЗ.

        Удаляется только то, по чему ничего не происходило: ни попыток,
        ни расхода, ни сессии агента. Сирота с историей — это работа,
        за которую платили, и стирать её нельзя.
        """
        removed, kept = await manual.prune(project_id, apply=True)
        return {"ok": True, "removed": removed, "kept": kept,
                "message": (f"удалено сирот: {removed}"
                            + (f", оставлено с историей: {kept}" if kept else ""))}

    # ---------- доступы конкретного заказа ----------

    @app.post("/api/project/{project_id}/access/{item_id}")
    async def put_access_key(project_id: int, item_id: int,
                             payload: dict = Body(...)):
        """Владелец вставил настоящее значение доступа прямо на карточке.

        Пункт становится `verified` — автопроверки доступа у нас по-прежнему
        нет, `verified` ставит человек. Задачи, ждавшие ИМЕННО этот ключ,
        возвращаются на приёмку.
        """
        try:
            return {"ok": True,
                    **await autonomy.put_key(project_id, item_id,
                                             str(payload.get("value") or ""))}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/project/{project_id}/deletion")
    async def deletion_preview(project_id: int):
        """Что именно пропадёт. Показываем ДО удаления, а не после.

        Кнопка «удалить заказ» — единственное необратимое действие в этой
        панели, и нажимают её обычно не глядя. Пусть в подтверждении будут
        настоящие цифры, а не «вы уверены?».
        """
        from sqlalchemy import func

        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise HTTPException(404, "проекта нет")
            tasks = (await s.execute(
                select(func.count()).select_from(Task)
                .where(Task.project_id == project_id))).scalar() or 0
            msgs = (await s.execute(
                select(func.count()).select_from(ChatMessage)
                .where(ChatMessage.project_id == project_id))).scalar() or 0
            runs = (await s.execute(
                select(func.count()).select_from(Run).join(Task, Task.id == Run.task_id)
                .where(Task.project_id == project_id))).scalar() or 0
        folder = projectdir.find(project_id)
        return {"id": project_id, "title": p.title, "client": p.client,
                "tasks": tasks, "messages": msgs, "runs": runs,
                "cost": round(p.cost_usd or 0, 2),
                "folder": str(folder) if folder else ""}

    @app.post("/api/project/{project_id}/delete")
    async def delete_project(project_id: int, payload: dict = Body(None)):
        """Удалить заказ из базы.

        **Каталог проекта на диске НЕ трогаем.** Там код, который писал агент,
        и весит он гигабайты; стереть его нажатием кнопки — не то, что можно
        сделать по одному клику. Путь возвращается, чтобы удалить руками,
        если он и правда не нужен.
        """
        from sqlalchemy import delete as sql_delete

        if not (payload or {}).get("confirm"):
            raise HTTPException(400, "удаление требует подтверждения")
        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise HTTPException(404, "проекта нет")
            title = p.title
            ids = list((await s.execute(
                select(Task.id).where(Task.project_id == project_id))).scalars().all())
            if ids:
                await s.execute(sql_delete(Run).where(Run.task_id.in_(ids)))
            for model in (Task, Message, AccessItem, ProjectChat, ChatMessage):
                await s.execute(sql_delete(model).where(model.project_id == project_id))
            await s.execute(sql_delete(Project).where(Project.id == project_id))
            await s.commit()
        folder = projectdir.find(project_id)
        log.warning("проект %s (%s) удалён из базы через дашборд; каталог %s "
                    "оставлен на диске", project_id, title, folder or "—")
        return {"ok": True, "message":
                f"Заказ «{title}» удалён." + (f" Каталог остался на диске: {folder} "
                                              f"— удали руками, если не нужен."
                                              if folder else "")}

    @app.post("/api/project/{project_id}/unblock")
    async def unblock(project_id: int):
        """Вернуть заказ из `blocked` в работу.

        Возвращаем в `briefing`, а не сразу в `active`: остальные гейты
        никуда не делись, и пусть их проверит обычный ход, а не эта кнопка.
        Из `blocked` заказ выводит ЧЕЛОВЕК — так было и в CLI, кнопка лишь
        избавляет от похода в консоль.
        """
        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise HTTPException(404, "проекта нет")
            if p.status != "blocked":
                return {"ok": False, "message": f"заказ не заблокирован "
                                                f"(статус {p.status})"}
            p.status = "briefing"
            p.last_action = "возвращён в работу владельцем"
            p.updated_at = utcnow()
            await s.commit()
        log.warning("проект %s: blocked -> briefing вручную из дашборда", project_id)
        return {"ok": True, "message": "заказ возвращён в работу"}

    @app.post("/api/project/{project_id}/ready")
    async def set_ready(project_id: int, payload: dict = Body(None)):
        """Галочка «готов к работе» — подтверждение владельца, гейт 1 из трёх."""
        on = bool((payload or {}).get("on", True))
        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None:
                raise HTTPException(404, "проекта нет")
            p.ready_for_work = on
            p.updated_at = utcnow()
            await s.commit()
        return {"ok": True, "ready": on}

    @app.post("/api/project/{project_id}/autonomous")
    async def toggle_autonomous(project_id: int, payload: dict = Body(None)):
        """Режим самостоятельности: работать с заглушками вместо ключей."""
        on = bool((payload or {}).get("on", True))
        try:
            if on:
                return {"ok": True, "on": True, **await autonomy.enable(project_id)}
            return {"ok": True, "on": False, **await autonomy.disable(project_id)}
        except ValueError as e:
            raise HTTPException(404, str(e)) from None

    # ---------- секреты ----------

    @app.get("/api/secrets")
    async def secrets_list():
        return await secret_state()

    @app.post("/api/secrets")
    async def secrets_add(payload: dict = Body(...)):
        """Положить значение в хранилище. Обратно оно не отдаётся никогда."""
        name = str(payload.get("name") or "").strip()
        value = str(payload.get("value") or "")
        if not SECRET_NAME_RE.match(name):
            raise HTTPException(400, "имя: латиница, цифры, _ . - и не длиннее 64")
        if not value.strip():
            raise HTTPException(400, "пустое значение хранить незачем")
        if not vault.enabled:
            raise HTTPException(400,
                                "VAULT_KEY не задан — секреты хранить негде. "
                                "Заведи ключ: python scripts/vault_cli.py genkey")
        vault.set(name, value)
        log.info("секрет %s записан в хранилище через дашборд", name)
        return {"ok": True, "name": name, "state": await secret_state()}

    @app.post("/api/secrets/{name}/delete")
    async def secrets_delete(name: str):
        if not SECRET_NAME_RE.match(name):
            raise HTTPException(400, "негодное имя")
        removed = vault.delete(name) if vault.enabled else False
        return {"ok": removed, "state": await secret_state()}

    # ---------- общее ----------

    @app.post("/api/pause")
    async def pause(on: bool = True):
        if on:
            PAUSE_FILE.write_text("остановлено из дашборда\n", encoding="utf-8")
        elif PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
        return {"paused": PAUSE_FILE.exists()}

    return app


def _roles_from(payload: dict, parsed) -> dict[str, str]:
    """Роли из окна, с подстраховкой на случай неизвестного говорящего.

    Незнакомый — клиент: ошибка в эту сторону безопаснее, пункт всё равно
    проверяется по evidence. Обратная ошибка молча выбросила бы требование.
    """
    given = payload.get("roles") or {}
    out = intake.guess_roles(parsed)
    for who, role in given.items():
        if role in roles.ROLES:
            out[str(who)] = role
    return out


def _parse_deadline(value) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="локальный дашборд автопилота")
    ap.add_argument("--port", type=int, default=8787)
    # Только петля. Наружу тут смотреть нечему: имена клиентов, суммы
    # и чеклисты — не то, что стоит отдавать в локальную сеть
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("нужен uvicorn: pip install fastapi uvicorn", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
                        datefmt="%H:%M:%S")
    print(f"дашборд: http://{args.host}:{args.port}")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
