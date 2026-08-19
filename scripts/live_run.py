"""ПЕРВЫЙ ЖИВОЙ ПРОГОН исполнителя: Executor → Verifier по одной задаче.

    python scripts/live_run.py --task 512 --task 513 --task 514
    python scripts/live_run.py --project 8 --list

Полоса `build` до сих пор запускала только заглушки. Здесь настоящий
Claude Code пишет код в изолированном каталоге, а настоящий судья его
принимает.

**Гейты обходятся ЗДЕСЬ, а не в коде.** `ready_for_work`, чеклист доступов
и `brief_ready` держат полосу build не просто так: они защищают клиента от
работы по недопонятому ТЗ и от начала без доступов. Снимать их в самом
планировщике ради одного прогона — значит сломать защиту навсегда. Поэтому
скрипт зовёт `Executor.run` и `Verifier.run` напрямую, мимо планировщика
полос: гейты остаются на месте и продолжают работать для всех остальных.

Клиенту при этом не уходит ничего: сообщения клиенту отправляет
`Communicator` из планировщика, а его тут нет вовсе.

Работа идёт в ОТДЕЛЬНОМ каталоге со своим git-репозиторием. Это не
придирка: `workspaces/p8` лежит внутри репозитория автопилота, и `git diff`
там показал бы правки самого автопилота — судья ревьюил бы чужую работу.
"""
from __future__ import annotations

import argparse
import contextlib
import asyncio
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import select                                          # noqa: E402

from autopilot import guard                                           # noqa: E402
from autopilot.config import cfg                                      # noqa: E402
from autopilot.db import (Project, Run, Session, Task, cli_spent_today,  # noqa: E402
                          init_db, utcnow)
from autopilot.executor import Executor                               # noqa: E402
from autopilot.llm import LimitReached, LLMError                      # noqa: E402
from autopilot.manual import NEEDS_HUMAN, needs_human                 # noqa: E402
from autopilot.textenc import decode_console                          # noqa: E402
from autopilot.verifier import Verifier                               # noqa: E402

LINE = "─" * 78


def prepare_workspace(project_id: int) -> Path:
    """Изолированный каталог со своим git. Без него судья смотрит не туда."""
    root = cfg.workspaces / f"p{project_id}-live"
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        for cmd in (["git", "init", "-q"],
                    ["git", "config", "user.email", "autopilot@local"],
                    ["git", "config", "user.name", "qorsa-autopilot"]):
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)
    return root


async def spent() -> float:
    return await cli_spent_today()


def _needs_server(task: Task) -> str | None:
    """Порт, в который стучится http-критерий задачи, если он есть."""
    import re
    for check in task.acceptance or []:
        if isinstance(check, dict) and check.get("type") == "http":
            m = re.search(r"localhost:(\d+)|127\.0\.0\.1:(\d+)",
                          str(check.get("url") or ""))
            if m:
                return m.group(1) or m.group(2)
    return None


@contextlib.asynccontextmanager
async def _server_if_needed(task: Task, workspace: Path):
    """Поднять бэкенд агента на время приёмки, если критерий требует localhost.

    Точка входа берётся стандартная для зафиксированного стека —
    `app.main:app` через uvicorn в окружении проекта. Это не догадка о чужом
    проекте: стек выбрал владелец, а `.venv` создал сам агент. Не удалось
    поднять — приёмка просто увидит закрытый порт, как и раньше.
    """
    port = _needs_server(task)
    backend = workspace / "backend"
    venv_py = None
    for sub in ("Scripts", "bin"):
        cand = backend / ".venv" / sub / ("python.exe" if sub == "Scripts" else "python")
        if cand.exists():
            venv_py = cand
            break
    if not port or venv_py is None or not (backend / "app").exists():
        yield
        return

    print(f"  поднимаю сервер агента для приёмки: uvicorn app.main:app :{port}")
    proc = await asyncio.create_subprocess_exec(
        str(venv_py), "-m", "uvicorn", "app.main:app", "--port", port,
        "--host", "127.0.0.1", cwd=str(backend),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        import httpx

        # НАСТОЯЩИЙ readiness-поллинг: опрашиваем порт, пока не ответит или
        # не истечёт READY_TIMEOUT. Отказ соединения в первую секунду — это
        # «ещё не встал», а не «не работает»: TCP-порт не слушается, пока
        # uvicorn не закончил импорт приложения, а импорт занимает время.
        # Раньше проверка стучалась один раз сразу — задача 499 падала не
        # по своей вине, приёмка ловила именно это окно.
        READY_TIMEOUT, POLL_EVERY = 30.0, 0.5
        deadline = asyncio.get_event_loop().time() + READY_TIMEOUT
        ready = False
        while asyncio.get_event_loop().time() < deadline:
            if proc.returncode is not None:
                err = decode_console((await proc.stderr.read())[-400:])
                print(f"  сервер не поднялся: {err}")
                break
            try:
                async with httpx.AsyncClient(timeout=1) as c:
                    await c.get(f"http://127.0.0.1:{port}/")
                ready = True
                break
            except Exception:
                await asyncio.sleep(POLL_EVERY)
        if ready:
            print("  сервер отвечает")
        elif proc.returncode is None:
            print(f"  сервер не ответил за {READY_TIMEOUT:.0f}с — критерии "
                 f"всё равно проверю, но порт скорее всего ещё закрыт")
        yield
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def run_task(task_id: int, workspace: Path) -> dict:
    """Одна задача через полный цикл. Возвращает сводку по итерациям."""
    async with Session() as s:
        task = await s.get(Task, task_id)
        project = await s.get(Project, task.project_id) if task else None
    if task is None or project is None:
        return {"task": task_id, "error": "нет такой задачи"}

    print(f"\n{LINE}\nЗАДАЧА {task.id}: {task.title}\n{LINE}")
    print(f"класс {task.verify_class}, исполнитель {task.executor}, "
          f"критериев {len(task.acceptance or [])}")
    for check in task.acceptance or []:
        detail = (check.get("cmd") or check.get("url") or check.get("path")
                  or check.get("criteria") or "")
        print(f"   · {check.get('type')}: {str(detail)[:100]}")

    executor, verifier = Executor(), Verifier()
    iterations: list[dict] = []

    for attempt in range(1, cfg.max_attempts + 1):
        print(f"\n  ── итерация {attempt} ──")
        before = await spent()
        t0 = time.monotonic()

        async with Session() as s:
            row = await s.get(Task, task_id)
            row.status = "running"
            await s.commit()
            fresh = await s.get(Task, task_id)
            proj = await s.get(Project, project.id)

        try:
            await executor.run(fresh, proj)
        except LimitReached as e:
            print(f"  УПОР В КВОТУ: {e}")
            return {"task": task_id, "title": task.title, "outcome": "квота",
                    "iterations": iterations}
        except Exception as e:                                    # noqa: BLE001
            build_sec = time.monotonic() - t0
            print(f"  сборка не удалась за {build_sec:.0f}с: {str(e)[:200]}")
            iterations.append({"n": attempt, "build": "ошибка",
                               "detail": str(e)[:200], "seconds": build_sec,
                               "quota": await spent() - before})
            continue

        build_sec = time.monotonic() - t0
        print(f"  сборка: {build_sec / 60:.1f} мин")

        # Сервер для http-критериев поднимаем МЫ, на время приёмки. Это работа
        # тест-стенда, а не верификатора: агент написал приложение, но его
        # процесс закончился вместе со сборкой, а критерий стучится в живой
        # localhost. Ровно на этом застряла задача 499 в прошлый раз.
        async with _server_if_needed(fresh, workspace):
            try:
                verdict = await verifier.run(fresh, proj)
            except LimitReached as e:
                print(f"  УПОР В КВОТУ на приёмке: {e}")
                return {"task": task_id, "title": task.title, "outcome": "квота",
                        "iterations": iterations}

        seconds = time.monotonic() - t0
        quota = await spent() - before
        print(f"  приёмка: {verdict.summary()}")
        for d in verdict.defects:
            print(f"    ✗ {str(d)[:200]}")
        for o in verdict.observations:
            print(f"    · вне критерия: {str(o)[:160]}")

        step = {"n": attempt, "ok": verdict.ok, "confirmed": verdict.confirmed,
                "unproven": verdict.unproven, "defects": list(verdict.defects),
                "observations": list(verdict.observations),
                "seconds": seconds, "quota": quota}
        iterations.append(step)

        if verdict.confirmed:
            await _finish(task_id, "done")
            print(f"  ✓ ПРИНЯТО автоматически за {seconds / 60:.1f} мин, "
                  f"квота ~${quota:.2f}")
            return {"task": task_id, "title": task.title, "outcome": "done",
                    "iterations": iterations}
        if verdict.unproven:
            await _finish(task_id, NEEDS_HUMAN, verdict.defects)
            print(f"  ~ ЖДЁТ ПОДТВЕРЖДЕНИЯ: {verdict.why_unproven()}")
            return {"task": task_id, "title": task.title, "outcome": "needs_human",
                    "iterations": iterations}
        if needs_human(verdict.defects):
            await _finish(task_id, NEEDS_HUMAN, verdict.defects)
            print("  ~ ПРОВЕРЯТЬ НЕЧЕМ — повторять бессмысленно")
            return {"task": task_id, "title": task.title, "outcome": "needs_human",
                    "iterations": iterations}

        await _bump(task_id, verdict.defects)
        print(f"  ✗ не принято, попытка {attempt} из {cfg.max_attempts}")

    await _finish(task_id, "escalated")
    print(f"  ✗✗ ЭСКАЛАЦИЯ после {cfg.max_attempts} попыток")
    return {"task": task_id, "title": task.title, "outcome": "escalated",
            "iterations": iterations}


async def _finish(task_id: int, status: str, defects=None) -> None:
    async with Session() as s:
        row = await s.get(Task, task_id)
        row.status = status
        if defects is not None:
            row.defects = list(defects)
        row.updated_at = utcnow()
        await s.commit()


async def _bump(task_id: int, defects) -> None:
    async with Session() as s:
        row = await s.get(Task, task_id)
        row.attempts += 1
        row.defects = list(defects)
        row.status = "ready"
        row.updated_at = utcnow()
        await s.commit()


async def show_tasks(project_id: int) -> int:
    await init_db()
    async with Session() as s:
        rows = (await s.execute(
            select(Task).where(Task.project_id == project_id,
                               Task.lane.in_(("build", "verify")),
                               Task.orphaned.is_(False))
            .order_by(Task.order_idx, Task.id))).scalars().all()
    print("id     класс      кто        зависимостей  задача")
    for t in rows:
        print(f"{t.id:<6} {t.verify_class:<10} {t.executor:<10} "
              f"{len(t.depends_on or []):<13} {t.title[:60]}")
    return 0


async def main_run(task_ids: list[int], project_id: int) -> int:
    await init_db()
    workspace = prepare_workspace(project_id)
    async with Session() as s:
        p = await s.get(Project, project_id)
        p.workspace = str(workspace)
        await s.commit()

    print(f"{LINE}\nПЕРВЫЙ ЖИВОЙ ПРОГОН ИСПОЛНИТЕЛЯ\n{LINE}")
    print(f"каталог: {workspace}  (отдельный git, вне репозитория автопилота)")
    print(f"модель исполнителя: {cfg.cc_model}, ходов до {cfg.max_turns}, "
          f"таймаут {cfg.task_timeout_sec}с")
    print(f"попыток на задачу: {cfg.max_attempts}")
    print(f"расход подписки за сутки до старта: ~${await spent():.2f} "
          f"из ${cfg.daily_cli_budget_usd:.0f}")
    print("гейты обойдены ТОЛЬКО здесь: задачи запускаются напрямую, "
          "мимо планировщика полос")
    print("клиенту ничего не уходит: Communicator в этом прогоне не участвует")

    started = await spent()
    results = []
    for task_id in task_ids:
        results.append(await run_task(task_id, workspace))

    print(f"\n{LINE}\nИТОГ\n{LINE}")
    done = sum(1 for r in results if r.get("outcome") == "done")
    total_quota = await spent() - started
    for r in results:
        steps = r.get("iterations") or []
        mark = {"done": "✓", "needs_human": "~", "escalated": "✗"}.get(
            r.get("outcome"), "?")
        secs = sum(s.get("seconds", 0) for s in steps)
        print(f"  {mark} {r.get('outcome', 'ошибка'):12s} итераций {len(steps)}, "
              f"{secs / 60:5.1f} мин — {str(r.get('title'))[:50]}")
    print(f"\n  закрылось само: {done} из {len(results)}")
    print(f"  квота на прогон: ~${total_quota:.2f}, "
          f"в среднем ~${total_quota / max(1, len(results)):.2f} на задачу")
    print(f"  расход подписки за сутки: ~${await spent():.2f} "
          f"из ${cfg.daily_cli_budget_usd:.0f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="живой прогон исполнителя по задачам")
    ap.add_argument("--task", type=int, action="append", default=[])
    ap.add_argument("--project", type=int, default=8)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list or not args.task:
        return guard.run(show_tasks(args.project))
    try:
        return guard.run(main_run(args.task, args.project))
    except guard.Interrupted as e:
        print(f"\n{e}. Дочерний claude убит.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
