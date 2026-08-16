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

from autopilot import checks                                       # noqa: E402
from autopilot import guard                                        # noqa: E402
from autopilot.config import cfg
from autopilot.llm import LimitReached, LLMError                                      # noqa: E402
from autopilot.db import consumed_today, AccessItem, ChatMessage, Project, Session, Task, init_db  # noqa: E402
from autopilot.planner import DETERMINISTIC_CHECKS, Planner           # noqa: E402
from autopilot.vault import (anthropic_key, anthropic_key_source,     # noqa: E402
                             missing_secret_message)

LINE = "─" * 78
CLASS_MARK = {"auto": "✓ авто", "assisted": "~ с моделью", "human": "✗ только глазами"}
EXEC_MARK = {"claude_code": "агент", "manual": "руками", "external": "третья сторона"}


def show_stack(decision: dict, path: str | None) -> None:
    """Выбранный стек — ПЕРВЫМ блоком, до задач.

    Это решение определяет весь остальной план: первая задача заводит проект
    на выбранных технологиях, а от неё зависят все прочие. Смотреть на него
    надо раньше, чем на список задач, а не выискивать в описаниях.
    """
    print(f"\n{LINE}\nВЫБРАННЫЙ СТЕК\n{LINE}")
    if not decision:
        print("  (модель не вернула решение по стеку)")
        return
    if decision.get("from_brief"):
        print("  ВЗЯТ ИЗ ТЗ — планировщик его не выбирал")
    for item in decision.get("chosen") or []:
        print(f"  • {item}")
    if decision.get("rationale"):
        print(f"\n  Почему: {decision['rationale']}")
    driven = decision.get("driven_by") or []
    if driven:
        print(f"  Продиктовано: {', '.join(str(d) for d in driven)}")
    rejected = decision.get("rejected") or []
    if rejected:
        print("\n  Отброшено:")
        for item in rejected:
            if isinstance(item, dict):
                print(f"    ✗ {item.get('option')} — {item.get('why')}")
            else:
                print(f"    ✗ {item}")
    if path:
        print(f"\n  записано в {path}")


def show_frame(brief: dict) -> None:
    """Рамки проекта: срок и бюджет. Внутри них выбиралось решение."""
    rows = []
    for field, label in (("deadline", "Срок"), ("budget", "Бюджет")):
        item = brief.get(field)
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            mark = "  [нет в последнем прогоне]" if item.get("missing") else ""
            rows.append(f"  {label}: {item['text']}{mark}")
    if not rows:
        print("\n  РАМКИ: срок и бюджет в ТЗ не названы — решение выбиралось "
              "без них")
        return
    print("\n  РАМКИ ПРОЕКТА:")
    for row in rows:
        print(row)


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
            # критерий обязан ПАДАТЬ на состоянии «задача не сделана».
            # Тот, что проходит на пустом проекте, создаёт видимость приёмки
            why = checks.suspicious(check)
            if why:
                print(f"       ⚠ пройдёт и без этой задачи: {why}")


def show_from_goal(tasks: list[dict]) -> None:
    """Задачи, выведенные из ЦЕЛИ, а не из пункта ТЗ — отдельно, на проверку.

    Ссылка на цель законна: именно из-за её запрета из плана однажды пропала
    публикация в App Store и Google Play, то есть то, чем проект кончается.
    Но основание это более слабое: цель широкая, и под неё удобно подвести
    работу, которой никто не просил. Поэтому такие задачи смотрят глазами.
    """
    rows = [t for t in tasks if t.get("ref_origin") == "goal"]
    print(f"\n{LINE}\nЗАДАЧИ ИЗ ЦЕЛИ, А НЕ ИЗ ПУНКТА ТЗ ({len(rows)}) — "
          f"ПРОВЕРЬ ГЛАЗАМИ\n{LINE}")
    if not rows:
        print("  (нет: все задачи выведены из конкретных пунктов ТЗ)")
        return
    for t in rows:
        print(f"  • {t['title']}")
        print(f"      обосновано целью: «{t['deliverable_ref'][:100]}»")
    print("\n  Цель — не выдумка, но и не пункт ТЗ. Смотри, действительно ли")
    print("  работа следует из неё, а не притянута.")


def show_suspicious(tasks: list[dict]) -> None:
    """Отдельным блоком — иначе тонет в общем выводе плана.

    Это эвристика и подсказка человеку, а не запрет: код такие критерии
    не выбрасывает. Смысл списка — показать, где зелёная приёмка ничего
    не доказывает, ДО того, как по плану начнут работать.
    """
    rows = []
    for t in tasks:
        for check in t["acceptance"]:
            why = checks.suspicious(check)
            if why:
                rows.append((t["title"], check.get("type"), why))

    print(f"\n{LINE}\nКРИТЕРИИ, КОТОРЫЕ ПРОЙДУТ НА ПУСТОМ ПРОЕКТЕ ({len(rows)})\n{LINE}")
    if not rows:
        print("  (не нашлось — но эвристика ловит только грубые случаи)")
        return
    for title, kind, why in rows:
        print(f"  ⚠ {title}")
        print(f"      {kind}: {why}")
    checked = sum(len(t["acceptance"]) for t in tasks)
    print(f"\n  {len(rows)} из {checked} критериев ничего не доказывают")


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
    # Две цифры отвечают на разные вопросы, и одна без другой врёт.
    # Пятнадцать мелких ручных задач против одной большой автоматической
    # дают 7% по количеству и могут давать 70% по времени
    print(f"\n  автономно закрывается:")
    print(f"    по числу задач:  {stats['auto_ratio'] * 100:3.0f}%   "
          f"({stats['autonomous']} из {total} задач)")
    print(f"    по времени:      {stats['auto_ratio_time'] * 100:3.0f}%   "
          f"({stats['minutes_auto']} из {stats['minutes']} мин)")
    if not stats["minutes"]:
        print("      (оценок времени в плане нет — вторая цифра не считается)")
    print(f"\n  порог {cfg.autonomy_min_ratio * 100:.0f}%, берётся ЛУЧШАЯ из двух "
          f"— {verdict}")
    print("  считается как задачи, которые агент и сделает, и проверит сам")
    print("  (assisted не входит: там вердикт выносит модель, и такая задача\n"
          "   не закрывается без подтверждения владельца)")


def show_notes(notes: list[str]) -> None:
    print(f"\n{LINE}\nПРАВКИ КОДА ПОВЕРХ ОТВЕТА МОДЕЛИ ({len(notes)})\n{LINE}")
    if not notes:
        print("  (нет: модель попала в схему с первого раза)")
    for note in notes:
        print(f"  • {note}")



async def show_spend() -> None:
    """Две цифры РАЗДЕЛЬНО: деньги и подписка. Это разные ресурсы."""
    both = await consumed_today()
    print(f"\n{LINE}\nРАСХОД ЗА СУТКИ\n{LINE}")
    print(f"  реальные деньги (API):  ${both['api_usd']:.2f}   "
          f"вызовов {int(both['api_calls'])}")
    print(f"  подписка (CLI):         ~${both['cli_usd_est']:.2f} оценочно, "
          f"вызовов {int(both['cli_calls'])}, {both['cli_seconds'] / 60:.1f} мин")
    print("  суточный бюджет считает ТОЛЬКО первую строку: подписка деньгами")
    print("  не тратится, и останавливать работу из-за неё было бы неверно")


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

    try:
        result = await Planner(client=None).plan(project)
    except guard.BusyProject as e:
        # Второй процесс на том же проекте — это не «занято, подожди»,
        # а «иначе получится каша»: оба пишут задачи через _persist
        print(f"\n{e}", file=sys.stderr)
        return 4
    except LimitReached as e:
        print(f"\nКВОТА ПОДПИСКИ ИСЧЕРПАНА: {e}\n"
              f"План не тронут — повтори, когда откроется окно.", file=sys.stderr)
        return 3
    except LLMError as e:
        print(f"\nМОДЕЛЬ НЕДОСТУПНА: {e}", file=sys.stderr)
        return 2
    if result is None:
        print("\nплан не собран — смотри лог выше", file=sys.stderr)
        return 1

    show_stack(result.get("stack_decision") or {}, result.get("decisions_path"))
    show_frame(brief)
    show_tasks(result["tasks"])
    show_graph(result["tasks"], result["order"])
    show_from_goal(result["tasks"])
    show_suspicious(result["tasks"])
    show_notes(result["notes"])
    show_stats(result["stats"])
    await show_spend()
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
        return guard.run(show_projects())
    # guard.run: сигнал прерывает прогон предсказуемо и уносит с собой
    # дочерний claude. Оставленный в живых, он дожигает квоту в одиночку
    try:
        return guard.run(run(args.project))
    except guard.Interrupted as e:
        print(f"\n{e}. Дочерний claude убит, план не тронут.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
