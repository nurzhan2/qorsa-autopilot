"""Локальный дашборд: что происходит и что от меня ждут.

    python scripts/dashboard.py
    открыть http://127.0.0.1:8787

Одна страница без сборки, без npm и без фронтенд-фреймворков: FastAPI отдаёт
HTML и JSON, страница раз в три секунды спрашивает состояние обычным fetch.

**Слушает только localhost.** Здесь видны имена клиентов, суммы и то, чего мы
ждём от людей; в локальной сети этому делать нечего. Значения секретов сюда
не попадают никогда — в базе их и нет, там только ссылки `{{SECRET:NAME}}`.

Порядок блоков не косметика: **сначала то, что ждёт человека**. Задача
в `needs_human` или `escalated` стоит на месте до решения владельца, и если
она утонет в общем списке, автопилот тихо встанет.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import select                                          # noqa: E402

from autopilot import manual                                          # noqa: E402
from autopilot.config import cfg                                      # noqa: E402
from autopilot.db import (Account, Project, Run, Session, Task,       # noqa: E402
                          AccessItem, cli_spent_today, consumed_today,
                          init_db, spent_today, utcnow)
from autopilot.verifier import Verifier                               # noqa: E402

# Пауза — файл, а не поле в базе: её должен уметь снять человек, даже если
# процесс автопилота лежит. Планировщик читает тот же файл.
PAUSE_FILE = ROOT / "PAUSED"

STATUS_RU = {
    "new": "новый", "briefing": "собираем ТЗ", "active": "в работе",
    "review": "на приёмке", "done": "сдан", "blocked": "заблокирован",
    "blocked_access": "ждём доступы",
}
TASK_RU = {
    "pending": "ждёт", "ready": "в очереди", "running": "выполняется",
    "done": "готово", "escalated": "эскалация", "needs_human": "нужен человек",
}


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
        access = (await s.execute(
            select(AccessItem).where(AccessItem.status != "verified"))).scalars().all()

    by_project: dict[int, list[Task]] = {}
    for t in tasks:
        by_project.setdefault(t.project_id, []).append(t)
    waiting_access: dict[int, list[str]] = {}
    for a in access:
        waiting_access.setdefault(a.project_id, []).append(f"{a.kind}: {a.name}")

    def task_row(t: Task) -> dict:
        return {
            "id": t.id, "title": t.title, "status": t.status,
            "status_ru": TASK_RU.get(t.status, t.status),
            "verify_class": t.verify_class, "executor": t.executor,
            "attempts": t.attempts, "orphaned": bool(t.orphaned),
            "defects": list(t.defects or []),
            "observations": list(t.observations or []),
            "waits_confirmation": manual.waits_for_confirmation(t),
            "project_id": t.project_id,
        }

    rows = []
    for p in projects:
        mine = by_project.get(p.id, [])
        live = [t for t in mine if not t.orphaned]
        done = [t for t in live if t.status == "done"]
        rows.append({
            "id": p.id,
            "company": (accounts.get(p.account_id).title
                        if accounts.get(p.account_id) else "—"),
            "client": p.client, "title": p.title,
            "status": p.status, "status_ru": STATUS_RU.get(p.status, p.status),
            "progress": f"{len(done)}/{len(live)}" if live else "—",
            "percent": round(len(done) / len(live) * 100) if live else 0,
            "cost": round(p.cost_usd or 0, 2),
            "waiting": waiting_access.get(p.id, []),
            "brief_ready": bool(p.brief_ready),
            "ready_for_work": bool(p.ready_for_work),
            "autonomy": round((p.autonomy_ratio or 0) * 100),
            "last_action": p.last_action or "",
            "tasks": [task_row(t) for t in live],
        })

    attention = [task_row(t) for t in tasks
                 if not t.orphaned and t.status in ("needs_human", "escalated")]
    both = await consumed_today()
    return {
        "projects": rows,
        "attention": attention,
        "runs": [{
            "id": r.id, "task_id": r.task_id, "kind": r.kind, "ok": bool(r.ok),
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
        "now": str(utcnow())[:19],
    }


def build_app():
    import contextlib

    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Схему поднимают миграции: дашборд может оказаться первым, кто
        # открыл базу после обновления кода
        await init_db()
        yield

    app = FastAPI(title="Qorsa Autopilot", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    async def state():
        return JSONResponse(await collect())

    @app.post("/api/task/{task_id}/done")
    async def mark_done(task_id: int):
        """«Сделал, проверь» — те же критерии, что и для агента."""
        ok, defects = await manual.submit(task_id, Verifier())
        return {"ok": ok, "defects": defects}

    @app.post("/api/task/{task_id}/confirm")
    async def confirm(task_id: int):
        """«Принимаю под свою ответственность» — только для needs_human."""
        ok, message = await manual.confirm(task_id)
        return {"ok": ok, "message": message}

    @app.post("/api/pause")
    async def pause(on: bool = True):
        if on:
            PAUSE_FILE.write_text("остановлено из дашборда\n", encoding="utf-8")
        elif PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
        return {"paused": PAUSE_FILE.exists()}

    return app


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

    print(f"дашборд: http://{args.host}:{args.port}")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
