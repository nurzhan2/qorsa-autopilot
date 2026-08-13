"""Прогон planner.py по реальному проекту с показом плана целиком.

    python scripts/plan_eval.py --list
    python scripts/plan_eval.py --project 2

Печатает задачи с классами проверяемости и критериями, граф зависимостей,
правки, которые внёс код поверх ответа модели, и долю работы, которую
система закроет без человека.

Заглушки здесь нет намеренно: смысл этого скрипта — увидеть, какие критерии
приёмки придумывает живая модель. Офлайн-экстрактор ответил бы на другой
вопрос.
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

from sqlalchemy import func, select                                   # noqa: E402

from autopilot.config import cfg                                      # noqa: E402
from autopilot.db import AccessItem, ChatMessage, Project, Session, Task, init_db  # noqa: E402
from autopilot.planner import DETERMINISTIC_CHECKS, Planner           # noqa: E402
from autopilot.vault import (anthropic_key, anthropic_key_source,     # noqa: E402
                             missing_secret_message)

LINE = "─" * 78
CLASS_MARK = {"auto": "✓ авто", "assisted": "~ с моделью", "human": "✗ только глазами"}
EXEC_MARK = {"claude_code": "агент", "manual": "руками", "external": "третья сторона"}


def show_tasks(tasks: list[dict]) -> None:
    print(f"\n{LINE}\nПЛАН ({len(tasks)} задач)\n{LINE}")
    for i, t in enumerate(tasks, 1):
        print(f"\n{i:2d}. {t['title']}")
        print(f"    класс: {CLASS_MARK.get(t['verify_class'], t['verify_class'])}"
              f"   |   делает: {EXEC_MARK.get(t['executor'], t['executor'])}"
              f"   |   ~{t['estimate_min']} мин")
        print(f"    из ТЗ: «{t['deliverable_ref']}»")
        if t["depends_on"]:
            print(f"    после: {', '.join(t['depends_on'])}")
        if t.get("description"):
            print(f"    что делать: {t['description'][:160]}")
        if t.get("risk"):
            print(f"    риск: {t['risk'][:160]}")
        if not t["acceptance"]:
            print("    приёмка: — НЕТ КРИТЕРИЕВ —")
        for check in t["acceptance"]:
            kind = check.get("type")
            mark = "  ⚙" if kind in DETERMINISTIC_CHECKS else "  ·"
            detail = (check.get("cmd") or check.get("url") or check.get("path")
                      or check.get("criteria") or "")
            selector = f" [{check['selector']}]" if check.get("selector") else ""
            expect = f" → {check['expect']}" if check.get("expect") else ""
            print(f"    {mark} {kind}: {detail}{selector}{expect}")


def show_graph(tasks: list[dict], order: list[str]) -> None:
    print(f"\n{LINE}\nГРАФ ЗАВИСИМОСТЕЙ\n{LINE}")
    deps = {t["title"]: t["depends_on"] for t in tasks}
    roots = [t for t in order if not deps.get(t)]
    print(f"  можно начинать сразу: {len(roots)}")
    for title in roots:
        print(f"    • {title}")
    linked = [t for t in order if deps.get(t)]
    if linked:
        print("\n  ждут другие задачи:")
        for title in linked:
            print(f"    • {title}  ← {', '.join(deps[title])}")
    else:
        print("\n  зависимостей нет ни у одной задачи")


def show_stats(stats: dict) -> None:
    print(f"\n{LINE}\nПОТОЛОК АВТОНОМНОСТИ\n{LINE}")
    total = stats["tasks"]
    print("  по классам проверяемости:")
    for name, count in stats["by_class"].items():
        share = count / total * 100 if total else 0
        print(f"    {CLASS_MARK.get(name, name):20s} {count:3d}  ({share:.0f}%)")
    print("  кто делает:")
    for name, count in stats["by_executor"].items():
        share = count / total * 100 if total else 0
        print(f"    {EXEC_MARK.get(name, name):20s} {count:3d}  ({share:.0f}%)")
    verdict = "годится для автопилота" if stats["suitable"] else "МАЛОПРИГОДЕН для автопилота"
    print(f"\n  автономно закрывается: {stats['auto_ratio'] * 100:.0f}% "
          f"(порог {cfg.autonomy_min_ratio * 100:.0f}%) — {verdict}")
    print("  считается как задачи, которые агент и сделает, и проверит сам")


def show_notes(notes: list[str]) -> None:
    print(f"\n{LINE}\nПРАВКИ КОДА ПОВЕРХ ОТВЕТА МОДЕЛИ ({len(notes)})\n{LINE}")
    if not notes:
        print("  (нет: модель попала в схему с первого раза)")
    for note in notes:
        print(f"  • {note}")


async def run(project_id: int) -> int:
    await init_db()
    async with Session() as s:
        project = await s.get(Project, project_id)
    if project is None:
        print(f"нет проекта {project_id}", file=sys.stderr)
        return 2

    brief = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
    if not brief or not brief.get("deliverables"):
        print("у проекта нет собранного брифа — сначала scripts/brief_eval.py",
              file=sys.stderr)
        return 2
    if not anthropic_key():
        print(missing_secret_message("ANTHROPIC_API_KEY"), file=sys.stderr)
        return 2

    print(f"\nПРОЕКТ {project.id}: {project.title} (клиент: {project.client})")
    print(f"[живая модель {cfg.plan_model}, ключ из: {anthropic_key_source()}]")
    print(f"пунктов ТЗ: {len(brief.get('deliverables') or [])}, "
          f"confidence {brief.get('confidence')}, готов={project.brief_ready}")

    result = await Planner(client=None).plan(project)
    if result is None:
        print("\nплан не собран — смотри лог выше", file=sys.stderr)
        return 1

    show_tasks(result["tasks"])
    show_graph(result["tasks"], result["order"])
    show_notes(result["notes"])
    show_stats(result["stats"])
    print()
    return 0


async def show_projects() -> int:
    await init_db()
    async with Session() as s:
        rows = (await s.execute(
            select(Project.id, Project.title, Project.brief_ready,
                   Project.autonomy_ratio, func.count(Task.id))
            .outerjoin(Task, (Task.project_id == Project.id) & (Task.lane == "build"))
            .group_by(Project.id).order_by(Project.id))).all()
    print("id   готов  автономность  задач  проект")
    for pid, title, ready, ratio, n in rows:
        print(f"{pid:<4} {str(bool(ready)):6s} {ratio or 0:>11.0%}  {n:>5}  {title}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="прогон planner.py по проекту")
    ap.add_argument("--project", type=int)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list or not args.project:
        return asyncio.run(show_projects())
    return asyncio.run(run(args.project))


if __name__ == "__main__":
    raise SystemExit(main())
