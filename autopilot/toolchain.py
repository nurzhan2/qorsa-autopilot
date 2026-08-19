"""Чем план собирается проверять и что для этого должно стоять на машине.

Автономность в 71% — арифметика, пока критерии требуют PostgreSQL, Flutter
SDK и поднятого сервера, а на машине нет ничего из этого. Первый живой прогон
это показал буквально: агент писал разумный код, а приёмка падала на
отсутствующем `alembic`, отсутствующем `flutter` и несуществующем
`localhost:8000`.

Поэтому перед тем, как отдать план в работу, код собирает из критериев список
нужных инструментов и сверяет с тем, что реально установлено. Недостающее
превращается в задачи подготовки окружения, и они идут ПЕРВЫМИ.

**Эти задачи — ручные.** Ставить SDK и поднимать базы агент не должен: он
уже пробовал и утянул Flutter SDK с Android SDK в рабочий каталог, раздув
его до 3.7 ГБ. Инструменты ставит владелец, системно и один раз.
"""
from __future__ import annotations

import re
import shutil

# Команда -> что это и как поставить. Ключ ищется как первое слово команды
# либо как отдельное слово внутри неё.
TOOLS: dict[str, tuple[str, str]] = {
    "python": ("Python", "python.org или winget install Python.Python.3"),
    "pip": ("pip", "идёт вместе с Python"),
    "pytest": ("pytest", "pip install pytest — в окружении проекта"),
    "alembic": ("Alembic", "pip install alembic — в окружении проекта"),
    "uvicorn": ("uvicorn", "pip install uvicorn — в окружении проекта"),
    "psql": ("PostgreSQL (клиент и сервер)", "postgresql.org или winget install PostgreSQL.PostgreSQL"),
    "pg_isready": ("PostgreSQL (клиент и сервер)", "идёт вместе с PostgreSQL"),
    "flutter": ("Flutter SDK", "docs.flutter.dev/get-started/install"),
    "dart": ("Dart SDK", "идёт вместе с Flutter"),
    "adb": ("Android SDK Platform Tools", "developer.android.com/studio"),
    "gradle": ("Gradle", "идёт вместе с Android Studio"),
    "xcodebuild": ("Xcode", "только на macOS, App Store"),
    "node": ("Node.js", "nodejs.org"),
    "npm": ("npm", "идёт вместе с Node.js"),
    "npx": ("npx", "идёт вместе с Node.js"),
    "docker": ("Docker", "docker.com/products/docker-desktop"),
    "git": ("Git", "git-scm.com"),
    "java": ("JDK", "adoptium.net"),
}

# Инструменты, которые ставятся В ПРОЕКТ, а не в систему: их отсутствие
# в PATH ничего не значит, потому что искать их надо в окружении проекта.
IN_PROJECT = {"pytest", "alembic", "uvicorn", "pip"}

# Фразы, которые не являются именем команды, но однозначно указывают на
# инструмент из TOOLS. Критерий «flutter build apk» не содержит слова adb,
# но без Android SDK не соберётся никогда — а именно так выглядел живой
# дефект задачи 536: `flutter build apk` упал, но дело не в коде клиента.
EXTRA_HINTS: dict[str, str] = {
    "apk": "adb",
    "aab": "adb",
    "android sdk": "adb",
    "android toolchain": "adb",
    "gradle": "gradle",
    ".ipa": "xcodebuild",
    "xcode": "xcodebuild",
}


def commands_in(cmd: str) -> set[str]:
    """Какие программы зовёт shell-команда.

    Грубо и намеренно: разбираем по разделителям конвейера и списка, берём
    первое слово каждого куска. Точный разбор шелла тут не нужен — нужен
    список того, что должно быть на машине.
    """
    found: set[str] = set()
    for part in re.split(r"\|\||&&|[|;&\n]", cmd or ""):
        words = part.strip().split()
        if not words:
            continue
        head = words[0].strip("\"'")
        # `cd backend && ...` — сам кусок ничего не требует, и аргумент его
        # командой не является: иначе каталог `backend` попадал бы в список
        # недостающих инструментов
        if head in ("cd", "set", "export", "echo", "true", "false", "pushd"):
            continue
        if not head:
            continue
        # `.venv/Scripts/python -m pytest` — критерий назвал окружение сам,
        # и это ровно то поведение, которого мы хотим: путь есть, вопросов нет
        if "/" in head or "\\" in head:
            continue
        found.add(head.lower().removesuffix(".exe"))
    return found


def needed_by(acceptance) -> set[str]:
    """Инструменты, которые требуются критериям приёмки задачи.

    Кроме имени команды, критерий может называть инструмент КОСВЕННО:
    `flutter build apk` не содержит слова `adb`, но без Android SDK не
    соберётся никогда. Живой случай: задача 536 попала в `needs_human` уже
    ПОСЛЕ провала — план не завёл для неё установочную задачу заранее, ровно
    потому что здесь стояла только буквальная `commands_in()`.
    """
    out: set[str] = set()
    for check in acceptance or []:
        if not isinstance(check, dict):
            continue
        if check.get("type") == "shell":
            cmd = str(check.get("cmd") or "")
            out |= commands_in(cmd)
            low = cmd.lower()
            for phrase, tool in EXTRA_HINTS.items():
                if phrase in low:
                    out.add(tool)
        elif check.get("type") in ("http", "dom", "screenshot"):
            # Проверка по адресу требует, чтобы кто-то этот адрес поднял.
            # Отдельным инструментом это не назвать, но задача «поднять
            # окружение» без запущенного сервера бессмысленна
            url = str(check.get("url") or "")
            if "localhost" in url or "127.0.0.1" in url:
                out.add("__server__")
    return out


# Технология в стеке -> что она требует на машине. Критерии называют не всё:
# «alembic upgrade head» не упоминает PostgreSQL, хотя без базы не работает,
# а `flutter build` не упоминает JDK. Стек знает это лучше команд.
STACK_TOOLS: tuple[tuple[str, str], ...] = (
    ("postgres", "psql"),
    ("postgresql", "psql"),
    ("flutter", "flutter"),
    ("react native", "node"),
    ("node", "node"),
    ("docker", "docker"),
    ("android", "adb"),
)


def needed_by_stack(brief: dict | None) -> set[str]:
    """Что требует сам стек проекта, даже если критерии об этом молчат."""
    from .brief import item_text

    text = " ".join(str(item_text(i)) for i in ((brief or {}).get("stack") or [])
                    if isinstance(i, dict)).lower()
    return {tool for name, tool in STACK_TOOLS if name in text}


def _server_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def installed(tool: str) -> bool:
    """Есть ли инструмент в системе — или, для сервера, доступен ли он.

    `psql` — особый случай. Проверять наличие клиента CLI неверно: приложение
    ходит в базу драйвером (asyncpg), а не через `psql`, и сервер вполне
    работает без установленного клиента. Живой случай проекта 8: PostgreSQL 17
    поднят на 5432, а `psql` в PATH нет — и без этой поправки код завёл бы
    ложную задачу «установить PostgreSQL», которая уже установлен и работает.

    Поэтому для базы спрашиваем не бинарь, а порт: отвечает ли кто-то на
    localhost:5432. Отвечает — считаем, что PostgreSQL есть.
    """
    if tool in ("psql", "pg_isready"):
        return shutil.which(tool) is not None or _server_reachable("localhost", 5432)
    return shutil.which(tool) is not None


def tool_hints_in_text(*texts: str) -> list[dict]:
    """Какие инструменты из TOOLS упоминает произвольный текст и сейчас
    отсутствуют на машине.

    Нужен дашборду: `needs_human`-задача, чей дефект называет отсутствующий
    инструмент, ждёт установки, а не разбора глазами — чинить код бессмысленно,
    пока инструмента нет, проверка всё равно не пройдёт.
    """
    low = " ".join(t for t in texts if t).lower()
    found: dict[str, None] = {}
    for key in TOOLS:
        if key in IN_PROJECT:
            continue
        if re.search(rf"\b{re.escape(key)}\b", low):
            found[key] = None
    for phrase, key in EXTRA_HINTS.items():
        if phrase in low and key in TOOLS:
            found[key] = None
    out = []
    for tool in found:
        if not installed(tool):
            name, how = TOOLS[tool]
            out.append({"tool": tool, "name": name, "how": how})
    return out


def missing_tools(tasks, brief: dict | None = None) -> list[dict]:
    """Чего не хватает на машине для проверки этого плана.

    Два источника, и оба нужны: команды из критериев и сам стек. Критерии
    называют не всё — `alembic upgrade head` не упоминает PostgreSQL, хотя
    без базы бесполезен.

    Инструменты уровня проекта (pytest, alembic) отсутствующими не считаются:
    их ставит сам агент внутрь `.venv`, и в системном PATH их быть не обязано.
    """
    wanted: dict[str, list[str]] = {}
    for task in tasks or []:
        title = str(task.get("title") or "")
        for tool in needed_by(task.get("acceptance")):
            wanted.setdefault(tool, []).append(title)
    for tool in needed_by_stack(brief):
        wanted.setdefault(tool, []).append("выбранный стек проекта")

    out = []
    for tool, titles in sorted(wanted.items()):
        if tool == "__server__":
            continue                      # это не инструмент, а состояние
        if tool in IN_PROJECT or tool not in TOOLS:
            continue
        if installed(tool):
            continue
        name, how = TOOLS[tool]
        out.append({"tool": tool, "name": name, "how": how,
                    "tasks": sorted(set(titles))})
    return out


def setup_tasks(missing: list[dict]) -> list[dict]:
    """Задачи подготовки окружения — ПЕРВЫМИ в плане и ТОЛЬКО руками.

    `executor="manual"` не обсуждается: агент уже пробовал поставить себе
    Flutter SDK и Android SDK и раздул рабочий каталог до 3.7 ГБ. Инструменты
    ставит владелец, системно и один раз.

    Критерий у такой задачи один и честный: инструмент отвечает на своё имя.
    """
    out = []
    for i, row in enumerate(missing):
        tool = row["tool"]
        out.append({
            "title": f"Окружение: установить {row['name']}",
            "description": (f"Поставить {row['name']} системно и убедиться, "
                            f"что `{tool}` доступен из командной строки.\n"
                            f"Как: {row['how']}.\n"
                            f"Без этого не проверить: "
                            + "; ".join(row["tasks"][:5])),
            "deliverable_ref": "",
            "acceptance": [{"type": "shell", "cmd": f"{tool} --version"}],
            "verify_class": "auto",
            "executor": "manual",
            "depends_on": [],
            "estimate_min": 30,
            "risk": "версия инструмента может не совпасть с ожиданиями плана",
            "order_hint": i,
            "setup": True,
        })
    return out
