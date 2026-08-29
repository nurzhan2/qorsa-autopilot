"""Каталог проекта: всё, что относится к одному заказу, лежит в одном месте.

До этого данные проекта были размазаны: код агента в `workspaces/pN`, логи
сессий в общем `logs/` вперемешку со всеми остальными, исходное ТЗ вообще
нигде — оно жило только строками в базе. Разобраться «что там по заказу»
можно было лишь через базу, а отдать заказ или заархивировать его было нечем.

Теперь у проекта один каталог:

    projects/p8-tur-tuk/
      workspace/     код, который пишет агент (его cwd)
      intake/        исходники ТЗ: выгрузка чата, документы клиента
      docs/          снимки brief.json и plan.json — читать глазами
      logs/          вывод сессий Claude Code по задачам этого проекта
      README.md      что за проект, когда заведён

Имя каталога — `p{id}-{слаг}`. Ведущий `p{id}` обязателен и неизменен: по нему
каталог находится, даже если проект переименовали. Слаг — латиницей, потому
что путь уходит в командную строку дочернего процесса, а кириллица в путях
на Windows однажды уже стоила нам вечера.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import cfg

log = logging.getLogger("projectdir")

# Транслитерация, а не «выбросить всё не-ASCII»: «Интернет-магазин» иначе
# превратился бы в пустую строку, и все такие проекты слиплись бы в `p7-`.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

MAX_SLUG = 40
SUBDIRS = ("workspace", "intake", "docs", "logs")


def slug(text: str) -> str:
    """Название проекта → кусок имени каталога: латиница, цифры, дефисы."""
    out = []
    for ch in (text or "").strip().lower():
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        else:
            out.append("-")
    s = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    return s[:MAX_SLUG].strip("-")


def root() -> Path:
    return cfg.projects_dir


def find(project_id: int) -> Path | None:
    """Каталог уже заведённого проекта или None.

    Ищем по префиксу `p{id}-`, а не по полному имени: проект могли
    переименовать после того, как каталог создан, и заводить второй каталог
    под тем же номером — верный способ потерять половину работы.
    """
    base = root()
    if not base.exists():
        return None
    exact = base / f"p{project_id}"
    if exact.is_dir():
        return exact
    for path in sorted(base.glob(f"p{project_id}-*")):
        if path.is_dir():
            return path
    return None


def ensure(project) -> Path:
    """Каталог проекта, создавая его при необходимости. Идемпотентно."""
    found = find(project.id)
    if found is None:
        name = f"p{project.id}"
        tail = slug(project.title or project.client)
        if tail:
            name = f"{name}-{tail}"
        found = root() / name
        found.mkdir(parents=True, exist_ok=True)
        log.info("каталог проекта %s заведён: %s", project.id, found)
    for sub in SUBDIRS:
        (found / sub).mkdir(parents=True, exist_ok=True)
    _write_readme(found, project)
    return found


def workspace(project) -> Path:
    """Рабочий каталог агента.

    Явно заданный `Project.workspace` сильнее: его ставит человек колонкой
    «Папка», и переносить чужой каталог мы не вправе.
    """
    explicit = (project.workspace or "").strip()
    if explicit:
        path = Path(explicit)
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = ensure(project) / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs(project) -> Path:
    return ensure(project) / "logs"


def intake(project) -> Path:
    return ensure(project) / "intake"


def docs(project) -> Path:
    return ensure(project) / "docs"


def save_snapshot(project, name: str, data) -> Path:
    """Снимок брифа или плана в docs/ — чтобы читать глазами, а не запросом.

    Это КОПИЯ, а не источник правды: правда живёт в базе. Файл нужен затем,
    чтобы можно было открыть каталог заказа и увидеть, по какому ТЗ его
    делали, — в том числе через месяц после сдачи.
    """
    import json

    path = docs(project) / name
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as e:
        log.warning("снимок %s не записан: %s", path, e)
    return path


def _write_readme(path: Path, project) -> None:
    """Шапка каталога. Перезаписываем на каждый вызов: название и клиент
    меняются, а файл этот читает человек, открывший папку через месяц."""
    body = (
        f"# {project.title or 'без названия'}\n\n"
        f"- проект: {project.id}\n"
        f"- клиент: {project.client or '—'}\n"
        f"- статус: {project.status}\n\n"
        "| папка | что внутри |\n"
        "|-------|------------|\n"
        "| `workspace/` | код проекта, здесь работает агент |\n"
        "| `intake/` | исходники ТЗ: выгрузка чата, документы клиента |\n"
        "| `docs/` | снимки собранного ТЗ и плана |\n"
        "| `logs/` | вывод сессий Claude Code по задачам |\n\n"
        "Источник правды — база `autopilot.db`. Файлы здесь — копии для чтения\n"
        "глазами, кроме `workspace/`: там живой код.\n"
    )
    try:
        (path / "README.md").write_text(body, encoding="utf-8")
    except OSError as e:
        log.debug("README проекта не записан: %s", e)
