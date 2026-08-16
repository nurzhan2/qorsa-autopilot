"""Прогон судьи по ОДНОЙ задаче с показом того, что он на самом деле ответил.

    python scripts/judge_eval.py --task 394
    python scripts/judge_eval.py --task 394 --diff tests/fixtures/judge/394.diff
    python scripts/judge_eval.py --list --project 8

Нужен затем же, зачем `brief_eval` и `plan_eval`: увидеть глазами, на какой
вопрос отвечает живая модель. Судья на задаче 394 вернул FAIL с девятью
дефектами, из которых ни один не был про заявленный критерий — на синтетике
это не показал бы ни один тест, потому что синтетика отвечает то, что в неё
положили.

Печатает раздельно:

* дефекты, ПРИВЯЗАННЫЕ к критерию — только они решают вердикт;
* замечания вне критерия — они не влияют ни на что, но их стоит прочитать;
* что именно код отбросил и почему.

`--diff` подставляет диф из файла вместо `git diff` в каталоге проекта.
Без него судить нечего там, где репозитория ещё нет, а проверить поведение
судьи надо.
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

from autopilot import guard                                            # noqa: E402
from autopilot import llm                                              # noqa: E402
from autopilot.config import cfg                                       # noqa: E402
from autopilot.db import (Project, Session, Task, consumed_today,      # noqa: E402
                          init_db)
from autopilot.llm import LimitReached, LLMError                       # noqa: E402
from autopilot.verifier import Verifier                                # noqa: E402

LINE = "─" * 78


def show_checks(task: Task) -> list[dict]:
    print(f"\n{LINE}\nКРИТЕРИИ ЗАДАЧИ\n{LINE}")
    llm_checks = []
    for check in task.acceptance or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("type") or "?")
        detail = (check.get("cmd") or check.get("url") or check.get("path")
                  or check.get("criteria") or "")
        print(f"  {kind:12s} {detail}")
        if kind == "llm":
            llm_checks.append(check)
    if not llm_checks:
        print("\n  llm-критериев нет — судью звать не за чем")
    return llm_checks


async def run(task_id: int, diff_path: str | None) -> int:
    await init_db()
    async with Session() as s:
        task = await s.get(Task, task_id)
        project = await s.get(Project, task.project_id) if task else None
    if task is None or project is None:
        print(f"нет задачи {task_id}", file=sys.stderr)
        return 2

    print(f"\nЗАДАЧА {task.id}: {task.title}")
    print(f"проект {project.id} ({project.title}), статус {task.status}, "
          f"попыток {task.attempts}")
    print(f"[судья: бэкенд {llm.backend_for('judge')}, модель "
          f"{cfg.cli_model if llm.backend_for('judge') == llm.CLI else cfg.judge_model}]")

    llm_checks = show_checks(task)
    if not llm_checks:
        return 1

    verifier = Verifier()
    if diff_path:
        body = Path(diff_path).read_text(encoding="utf-8")
        print(f"\nдиф взят из файла {diff_path} ({len(body)} символов)")

        async def _diff(_cwd):
            return body

        verifier._diff = _diff                       # noqa: SLF001

    for check in llm_checks:
        criteria = str(check.get("criteria") or "")
        print(f"\n{LINE}\nСУДЬЯ ОТВЕЧАЕТ НА ЭТОТ КРИТЕРИЙ\n{LINE}\n  {criteria}")
        try:
            ok, msg, notes = await verifier._judge(                   # noqa: SLF001
                criteria, str(cfg.workspaces / f"p{project.id}"), task, project)
        except LimitReached as e:
            print(f"\nКВОТА ПОДПИСКИ ИСЧЕРПАНА: {e}", file=sys.stderr)
            return 3
        except LLMError as e:
            print(f"\nМОДЕЛЬ НЕДОСТУПНА: {e}", file=sys.stderr)
            return 2

        print(f"\n  ВЕРДИКТ: {'PASS' if ok else 'FAIL'}")
        if msg:
            print(f"\n  ДЕФЕКТЫ ПО КРИТЕРИЮ (только они решают вердикт):")
            for part in str(msg).replace("судья: ", "").split("; "):
                print(f"    ✗ {part}")
        else:
            print("\n  дефектов по критерию нет")
        print(f"\n  ЗАМЕЧАНИЯ ВНЕ КРИТЕРИЯ ({len(notes)}) — на вердикт НЕ влияют:")
        for note in notes:
            print(f"    · {note}")
        if not notes:
            print("    (нет)")

    await show_spend()
    print()
    return 0


async def show_spend() -> None:
    both = await consumed_today()
    print(f"\n{LINE}\nРАСХОД ЗА СУТКИ\n{LINE}")
    print(f"  реальные деньги (API):  ${both['api_usd']:.2f}   "
          f"вызовов {int(both['api_calls'])}")
    print(f"  подписка (CLI):         ~${both['cli_usd_est']:.2f} оценочно, "
          f"вызовов {int(both['cli_calls'])}, {both['cli_seconds'] / 60:.1f} мин")


async def show_tasks(project_id: int | None) -> int:
    await init_db()
    async with Session() as s:
        q = select(Task).order_by(Task.project_id, Task.id)
        if project_id:
            q = q.where(Task.project_id == project_id)
        rows = (await s.execute(q)).scalars().all()
    print("id    проект  критериев  llm  задача")
    for t in rows:
        checks = [c for c in (t.acceptance or []) if isinstance(c, dict)]
        n_llm = sum(1 for c in checks if c.get("type") == "llm")
        if not n_llm:
            continue
        print(f"{t.id:<5} {t.project_id:<7} {len(checks):<10} {n_llm:<4} {t.title}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="прогон судьи по одной задаче")
    ap.add_argument("--task", type=int)
    ap.add_argument("--project", type=int, help="для --list")
    ap.add_argument("--list", action="store_true", help="задачи с llm-критериями")
    ap.add_argument("--diff", help="файл с дифом вместо git diff в каталоге проекта")
    args = ap.parse_args()
    if args.list or not args.task:
        return guard.run(show_tasks(args.project))
    try:
        return guard.run(run(args.task, args.diff))
    except guard.Interrupted as e:
        print(f"\n{e}. Дочерний claude убит.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
