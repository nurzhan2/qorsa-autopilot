"""Каталоги проектов: посмотреть и перевезти старые.

С появлением `autopilot/projectdir.py` данные заказа лежат в одном месте —
`projects/p{id}-{слаг}/`. До этого код жил в `workspaces/pN`, логи сессий
в общем `logs/`, а исходное ТЗ нигде. Новые проекты сразу заводятся по-новому,
а этот скрипт показывает старые и, если попросить, перевозит их.

    python scripts/projects_cli.py list                 # что где лежит
    python scripts/projects_cli.py migrate              # примерка, ничего не трогает
    python scripts/projects_cli.py migrate --apply      # перевезти
    python scripts/projects_cli.py migrate --apply --project 8

**По умолчанию — примерка.** Тот же порядок, что у `tasks_cli prune`: сначала
показать, что будет сделано, и только по явному `--apply` делать. Здесь это
важнее обычного — каталог рабочего проекта весит гигабайты, и «ой, не туда»
исправляется руками и долго.

Переезд НЕ трогает каталог, на который `Project.workspace` указывает явно
и который лежит вне `workspaces/`: его выбрал человек колонкой «Папка», и
перевозить чужое место мы не вправе. Такие показываются отдельной строкой.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import select                                       # noqa: E402

from autopilot import projectdir                                    # noqa: E402
from autopilot.config import cfg                                    # noqa: E402
from autopilot.db import Project, Session, init_db, utcnow          # noqa: E402


def human(size: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def legacy_dir(project: Project) -> Path | None:
    """Старый каталог проекта, если он есть и его можно перевозить.

    Перевозим только то, что лежит внутри `workspaces/`: это наше место,
    заведённое кодом. Путь, выставленный человеком куда-то ещё, — его дело.
    """
    explicit = (project.workspace or "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(cfg.workspaces / f"p{project.id}")
    for path in candidates:
        try:
            inside = path.resolve().is_relative_to(cfg.workspaces.resolve())
        except (OSError, ValueError):
            inside = False
        if path.is_dir() and inside and any(path.iterdir()):
            return path
    return None


def foreign_dir(project: Project) -> Path | None:
    """Каталог, выбранный человеком вне workspaces/. Показываем, но не трогаем."""
    explicit = (project.workspace or "").strip()
    if not explicit:
        return None
    path = Path(explicit)
    try:
        if path.resolve().is_relative_to(cfg.workspaces.resolve()):
            return None
    except (OSError, ValueError):
        pass
    return path if path.is_dir() else None


async def load_projects(only: int | None) -> list[Project]:
    await init_db()
    async with Session() as s:
        q = select(Project).order_by(Project.id)
        if only:
            q = q.where(Project.id == only)
        return list((await s.execute(q)).scalars().all())


async def cmd_list(args) -> int:
    projects = await load_projects(args.project)
    if not projects:
        print("проектов нет")
        return 0
    print(f"каталог проектов: {projectdir.root()}\n")
    for p in projects:
        folder = projectdir.find(p.id)
        print(f"#{p.id} {p.title or '—'} ({p.client or '—'}, {p.status})")
        print(f"   папка проекта: {folder or 'ещё не заведена'}")
        old = legacy_dir(p)
        if old:
            print(f"   старый каталог: {old} — {human(dir_size(old))}, ждёт переезда")
        stray = foreign_dir(p)
        if stray:
            print(f"   свой каталог (не трогаем): {stray}")
    return 0


async def cmd_migrate(args) -> int:
    projects = await load_projects(args.project)
    planned: list[tuple[Project, Path, Path]] = []
    for p in projects:
        old = legacy_dir(p)
        if old is None:
            continue
        target = projectdir.ensure(p) / "workspace"
        # Каталог назначения уже с содержимым — сливать вслепую нельзя:
        # два разных состояния одного проекта перемешаются молча
        if target.exists() and any(target.iterdir()):
            print(f"#{p.id}: ПРОПУСК — {target} уже не пуст, разберись руками")
            continue
        planned.append((p, old, target))

    if not planned:
        print("переезжать нечего")
        return 0

    for p, old, target in planned:
        print(f"#{p.id} {p.title}: {old}  ->  {target}  ({human(dir_size(old))})")
    if not args.apply:
        print(f"\nпримерка: {len(planned)} каталог(ов). Перевезти — повтори с --apply")
        return 0

    moved = 0
    for p, old, target in planned:
        try:
            # move, а не copy: две копии рабочего каталога на гигабайты —
            # это способ через месяц не помнить, какая из них живая
            if target.exists():
                target.rmdir()              # пустой, проверено выше
            shutil.move(str(old), str(target))
        except OSError as e:
            print(f"#{p.id}: не перевёз — {e}")
            continue
        async with Session() as s:
            row = await s.get(Project, p.id)
            if row is not None:
                row.workspace = str(target)
                row.updated_at = utcnow()
                await s.commit()
        moved += 1
        print(f"#{p.id}: перевезён, Project.workspace теперь {target}")
    print(f"\nперевезено: {moved} из {len(planned)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="каталоги проектов автопилота")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="что где лежит")
    p_list.add_argument("--project", type=int, default=None)
    p_list.set_defaults(fn=cmd_list)

    p_mig = sub.add_parser("migrate", help="перевезти старые каталоги")
    p_mig.add_argument("--project", type=int, default=None)
    p_mig.add_argument("--apply", action="store_true",
                       help="без него — только примерка")
    p_mig.set_defaults(fn=cmd_migrate)

    args = ap.parse_args()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
