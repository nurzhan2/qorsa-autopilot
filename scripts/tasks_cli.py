"""Ручные задачи: посмотреть список и отметиться выполненным.

    python scripts/tasks_cli.py list                 # всё, что ждёт рук
    python scripts/tasks_cli.py list --project 2
    python scripts/tasks_cli.py done 17              # «сделал» → машина проверит
    python scripts/tasks_cli.py show 17

Отметка «сделал» задачу не закрывает. Она запускает те же критерии приёмки,
что и для агента: если они не прошли, задача останется открытой с объяснением,
что именно не сошлось. На слово не верим ни агенту, ни себе — иначе критерии
превращаются в украшение.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from autopilot import manual                                    # noqa: E402
from autopilot.db import Session, Task, init_db                 # noqa: E402
from autopilot.verifier import Verifier                         # noqa: E402


async def cmd_list(project_id: int | None) -> int:
    await init_db()
    print(await manual.report(project_id))
    return 0


async def cmd_show(task_id: int) -> int:
    await init_db()
    async with Session() as s:
        task = await s.get(Task, task_id)
    if task is None:
        print(f"нет задачи {task_id}", file=sys.stderr)
        return 2
    print(manual.describe(task))
    print(f"\n    статус: {task.status}   исполнитель: {task.executor}   "
          f"класс: {task.verify_class}")
    if task.defects:
        print("    прошлые замечания:")
        for d in task.defects:
            print(f"      ✗ {d}")
    return 0


async def cmd_done(task_id: int) -> int:
    await init_db()
    print(f"проверяю задачу {task_id}...")
    ok, defects = await manual.submit(task_id, Verifier())
    if ok:
        print("ПРИНЯТО: все критерии прошли")
        return 0
    print("НЕ ПРИНЯТО:")
    for d in defects:
        print(f"  ✗ {d}")
    print("\nзадача осталась открытой — поправь и отметься снова")
    return 1


async def cmd_confirm(task_id: int) -> int:
    await init_db()
    ok, message = await manual.confirm(task_id)
    print(message)
    if ok:
        print("записано как закрытое ТОБОЙ, а не проверками — так и останется в истории")
    return 0 if ok else 2


async def cmd_prune(project_id: int, apply: bool) -> int:
    """Выбросить осиротевшие задачи, по которым ничего не происходило.

    Сироты — след прошлых прогонов планировщика: пока сопоставление шло
    по точному названию, каждый прогон осиротял предыдущий план целиком.
    Сама по себе сирота безвредна, но когда их вчетверо больше живых задач,
    список перестаёт читаться, а это единственный способ увидеть свою работу.
    """
    await init_db()
    safe, keep = await manual.prunable(project_id)
    if not safe and not keep:
        print(f"у проекта {project_id} осиротевших задач нет")
        return 0

    print(f"осиротевших задач: {len(safe) + len(keep)}")
    print(f"\nБЕЗ ИСТОРИИ — можно удалять ({len(safe)}):")
    for t in safe[:40]:
        print(f"  #{t.id} {t.title[:70]}")
    if len(safe) > 40:
        print(f"  ... и ещё {len(safe) - 40}")
    if keep:
        print(f"\nС ИСТОРИЕЙ — остаются, решать тебе ({len(keep)}):")
        for t in keep:
            why = []
            if (t.attempts or 0) > 0:
                why.append(f"попыток {t.attempts}")
            if t.status in ("done", "escalated", manual.NEEDS_HUMAN):
                why.append(t.status)
            if t.cc_session_id:
                why.append("была сессия агента")
            print(f"  #{t.id} {t.title[:60]} — {', '.join(why) or 'есть расход'}")

    if not apply:
        print(f"\nэто примерка. Удалить: --apply")
        return 0
    removed, kept = await manual.prune(project_id, apply=True)
    print(f"\nудалено {removed}, оставлено с историей {kept}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ручные задачи проекта")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="что ждёт рук")
    p_list.add_argument("--project", type=int, default=None)
    p_show = sub.add_parser("show", help="задача целиком")
    p_show.add_argument("task_id", type=int)
    p_done = sub.add_parser("done", help="отметить выполненной и проверить")
    p_done.add_argument("task_id", type=int)
    p_conf = sub.add_parser(
        "confirm", help="закрыть задачу под свою ответственность (assisted-приёмка)")
    p_conf.add_argument("task_id", type=int)
    p_prune = sub.add_parser("prune", help="выбросить осиротевшие задачи без истории")
    p_prune.add_argument("--project", type=int, required=True)
    p_prune.add_argument("--apply", action="store_true",
                         help="без него — только примерка")
    args = ap.parse_args()

    if args.cmd == "list":
        return asyncio.run(cmd_list(args.project))
    if args.cmd == "show":
        return asyncio.run(cmd_show(args.task_id))
    if args.cmd == "confirm":
        return asyncio.run(cmd_confirm(args.task_id))
    if args.cmd == "prune":
        return asyncio.run(cmd_prune(args.project, args.apply))
    return asyncio.run(cmd_done(args.task_id))


if __name__ == "__main__":
    raise SystemExit(main())
