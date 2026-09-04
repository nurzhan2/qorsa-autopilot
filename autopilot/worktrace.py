"""Какие файлы изменила ИМЕННО ЭТА сессия агента.

До этого модуля ответа на вопрос не было вообще. Агенту выдаются
`Bash,Read,Edit,Write,Glob,Grep` с `--permission-mode acceptEdits`, а
единственное ограничение — фраза в промпте «не трогай файлы вне текущего
каталога». Это ПРОСЬБА К МОДЕЛИ, то есть ровно то, что в этом проекте
не считается защитой нигде больше. `Executor.run` возвращает конверт CLI,
списка файлов в нём нет; `paths.of()` знает только пути ИЗ КРИТЕРИЕВ — то
есть те, что назвал планировщик, а не те, что агент правда тронул.

Способ выбран простейший из работающих: слепок каталога до сессии и после.

**Почему не git.** Репозиторий есть ровно в одном каталоге заказа из четырёх,
и коммитов в нём ноль — завёл его вспомогательный скрипт. `git diff` там
показал бы ВСЕ файлы проекта на любую задачу, то есть ответил бы неправду.
Заводить репозиторий самим — отдельное решение с другой ценой; слепок работает
везде и сразу.

**Границы, и обе честные:**

* слепок ловит ЗАПИСЬ, а не ЧТЕНИЕ. Рабочий каталог лежит внутри установки,
  `.env` — тремя каталогами выше, у агента есть `Bash`. Прочитать секрет и
  вписать его в отчёт слепок не увидит вовсе;
* `mtime`+`size` не ловят намеренную подделку: перезаписать файл тем же
  размером и вернуть `mtime` через `os.utime` можно. Против злого умысла
  это не рубеж, против ошибки — рубеж.

Замерено на рабочем каталоге проекта 8: 333 файла за 0.03 с с исключениями,
6654 файла (224 МБ) за 0.29 с без них.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("worktrace")

# Каталоги, которые агент наполняет не работой, а инструментами и сборками.
# Без них слепок проекта 8 — 6654 файла вместо 333, и каждая задача выглядела
# бы как правка тысяч файлов.
#
# `.git` здесь тоже: он меняется от любой команды git, а правил денилиста
# по нему нет — от той единственной идеи (запретить `.git/hooks`) пришлось
# отказаться, потому что она стояла на неверном факте: `git add -N .`,
# который делает наш верификатор, хуков НЕ запускает, а коммитов автопилот
# в рабочем каталоге не делает вовсе.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".gradle", ".dart_tool", ".idea",
    "build", "dist", ".next", ".nuxt", ".parcel-cache", "target",
    "Pods", "DerivedData", ".terraform", "vendor", "coverage", ".cache",
})

# Потолок на число файлов в слепке. Не про скорость: агент однажды утянул
# в рабочий каталог Flutter SDK и Android SDK и раздул его до 3.7 ГБ. Слепок
# такого каталога — не защита, а тормоз; лучше честно сказать, что не смотрели.
MAX_FILES = 40000


@dataclass(frozen=True)
class Snapshot:
    """Состояние каталога: путь -> (mtime_ns, размер)."""

    files: dict = field(default_factory=dict)
    ok: bool = True
    why: str = ""

    def __len__(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class Changed:
    """Разница двух слепков.

    `ok=False` означает «слепок неполон», а НЕ «изменений нет». Разница
    принципиальная: пустой список при `ok=False` нельзя прочитать как
    «нарушений не найдено» — именно так и выглядит молчаливый пропуск,
    которого этот проект избегает везде.
    """

    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    ok: bool = True
    why: str = ""

    @property
    def paths(self) -> tuple[str, ...]:
        """Все тронутые пути, отсортированные."""
        return tuple(sorted({*self.created, *self.modified, *self.deleted}))

    @property
    def touched(self) -> tuple[str, ...]:
        """Только правки и удаления существовавшего.

        Отдельно от `created`, потому что смысл разный: создать файл теста —
        это работа, а переписать существующий файл теста — это потеря
        доказательства (см. `evidence.py`).
        """
        return tuple(sorted({*self.modified, *self.deleted}))

    def __len__(self) -> int:
        return len(self.paths)


def snapshot(root) -> Snapshot:
    """Слепок рабочего каталога. Никогда не бросает: наблюдатель не вправе
    ронять работу, за которой наблюдает."""
    base = Path(root)
    if not base.is_dir():
        return Snapshot({}, ok=False, why=f"каталога нет: {base}")

    files: dict[str, tuple[int, int]] = {}
    problems: list[str] = []

    def on_error(exc: OSError) -> None:
        # Без этого os.walk пропускает нечитаемое поддерево МОЛЧА, и слепок
        # вернул бы ok=True — «нарушений нет» там, где на самом деле
        # «не смотрели». Ровно тот молчаливый пропуск, ради отказа от
        # которого модуль и написан.
        problems.append(f"{getattr(exc, 'filename', '?')}: {exc}")

    for folder, dirs, names in os.walk(base, onerror=on_error):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = Path(folder) / name
            try:
                st = path.stat()
            except OSError as e:
                problems.append(f"{path}: {e}")
                continue
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:                       # симлинк наружу
                problems.append(f"{path}: вне каталога проекта")
                continue
            files[rel] = (st.st_mtime_ns, st.st_size)
            if len(files) > MAX_FILES:
                return Snapshot(files, ok=False,
                                why=f"больше {MAX_FILES} файлов — слепок не снят целиком")

    if problems:
        return Snapshot(files, ok=False,
                        why=f"не прочитано {len(problems)}: {problems[0][:160]}")
    return Snapshot(files, ok=True)


def diff(before: Snapshot, after: Snapshot) -> Changed:
    """Что изменилось между двумя слепками."""
    why = before.why or after.why
    ok = before.ok and after.ok
    if not before.files and not before.ok:
        # Не с чем сравнивать: всё после сессии выглядело бы созданным ею
        return Changed(ok=False, why=why or "слепок «до» не снят")

    created, modified = [], []
    for path, stamp in after.files.items():
        was = before.files.get(path)
        if was is None:
            created.append(path)
        elif was != stamp:
            modified.append(path)
    deleted = [p for p in before.files if p not in after.files]
    return Changed(tuple(sorted(created)), tuple(sorted(modified)),
                   tuple(sorted(deleted)), ok=ok, why=why)


def around(root):
    """Контекстный менеджер: слепок до, слепок после, разница в `.changed`.

    Пользоваться так::

        with worktrace.around(ws) as trace:
            await executor.run(task, project)
        trace.changed.paths
    """
    return _Around(root)


class _Around:
    def __init__(self, root):
        self.root = root
        self.before = Snapshot({}, ok=False, why="слепок «до» не снят")
        self.changed = Changed(ok=False, why="сессия не закончилась")

    def __enter__(self):
        self.before = snapshot(self.root)
        if not self.before.ok:
            log.warning("слепок каталога %s неполон: %s", self.root, self.before.why)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.changed = diff(self.before, snapshot(self.root))
        return False                                  # исключение не глотаем
