"""Готова ли УСТАНОВКА к автономной работе — одним ответом, заранее.

Сейчас это выясняется провалом задачи, причём в самой дорогой форме: агент
отработал сессию, приёмка упала, `manual.needs_human` увёл задачу человеку —
и только тогда стало видно, что на машине нет браузера или инструмента.

Четыре проверки, и каждая закрывает свой способ тихо испортить работу:

* **`git`** — самая важная и самая незаметная. `Verifier._git` глотает
  `FileNotFoundError` и возвращает пустую строку, `_diff` отдаёт `""`,
  судья получает пустой диф и честно судит пустоту. Молча. Это ровно тот
  класс «выглядит как работа, а работы нет», против которого написан
  весь проект;
* **`claude`** — без него не поднимется ни одна сессия. Разбор пути тот же,
  что у исполнителя (`llm.resolve_cli`): npm на Windows ставит `claude.CMD`,
  и `create_subprocess_exec` обёртки исполнять не умеет;
* **`playwright`** — без него `dom` и `screenshot` честно падают, но узнаём
  мы это уже после сессии агента;
* **ключ Anthropic** — но только если судья настроен на API. По умолчанию
  всё идёт по подписке, и тогда пункт не блокирует ничего: называть
  установку негодной из-за ключа, который в рабочем режиме не нужен, —
  ложная тревога.

Блокируют работу первые два. Остальные мешают, и это разные вещи: пункт,
который ничего не останавливает, в списке «почему всё стоит» врёт.

Чего здесь НЕТ, хотя есть у прототипа (loop-engineering, `tools/loop-audit`):
**балла 0–100 и уровней L0–L3.** Тот скоринг меряет наличие файлов
в репозитории — 18 баллов за `STATE.md`, 15 за файлы чужой экосистемы, — а
не то, работает ли заказ. Балл усредняет несравнимое: отсутствие браузера
и отсутствие раздела в документации складываются в одно число, по которому
непонятно, что делать. Здесь вместо балла КОНЪЮНКТИВНЫЙ гейт: либо всё, что
нужно для автономной работы, на месте, либо названо поимённо то, чего нет.

Что уже проверяется в других местах и здесь не дублируется: недостающие
секреты и токены компаний — `dashboard.secret_state()`, инструменты задач
проекта — `toolchain.missing_tools()`.
"""
from __future__ import annotations

import logging
import sys
import shutil
from dataclasses import dataclass

from .config import cfg

log = logging.getLogger("readiness")


@dataclass(frozen=True)
class Item:
    key: str
    ok: bool
    what: str          # что не так
    fix: str           # что с этим сделать
    blocking: bool     # без этого автономная работа невозможна

    @property
    def title(self) -> str:
        return NAMES.get(self.key, self.key)


NAMES = {
    "claude": "Claude Code CLI",
    "git": "git",
    "playwright": "браузер для проверок dom и screenshot",
    "key": "ключ Anthropic",
}


def _claude() -> Item:
    from . import llm

    resolved, _ = llm.resolve_cli(cfg.claude_bin)
    return Item("claude", bool(resolved),
                what=f"не найден {cfg.claude_bin!r} — сессии агента не поднимутся",
                fix="npm i -g @anthropic-ai/claude-code, потом claude login",
                blocking=True)


def _git() -> Item:
    """Без git приёмка МОЛЧА судит пустоту.

    `Verifier._diff` собирает диф для судьи через `git`; когда git не найден,
    `_git` ловит `FileNotFoundError` и возвращает пустую строку. Судья
    получает пустой диф — и это не ошибка, а «проверять нечего».
    """
    found = shutil.which("git")
    return Item("git", bool(found),
                what="git не найден: судья будет получать ПУСТОЙ диф и судить "
                     "пустоту — молча, без ошибки",
                fix="поставь git (git-scm.com) и перезапусти автопилот",
                blocking=True)


def _browsers_dir():
    """Куда playwright кладёт браузеры. Проверяем СУЩЕСТВОВАНИЕМ, а не
    запуском — тот же приём, что у `toolchain` с юникс-утилитами.

    Запускать `sync_playwright()` здесь нельзя: проверка зовётся и из
    дашборда, и из главного цикла, а синхронный API playwright внутри
    работающего event loop падает. Диагностика, которая роняет то, что
    диагностирует, хуже отсутствующей.
    """
    import os
    from pathlib import Path

    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    home = Path.home()
    if os.name == "nt":
        return home / "AppData" / "Local" / "ms-playwright"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    return home / ".cache" / "ms-playwright"


def _playwright() -> Item:
    """Браузер нужен `dom` и `screenshot`. Проверяем не импорт пакета,
    а наличие самого браузера: `pip install playwright` ставит библиотеку,
    а chromium приезжает отдельной командой, и именно её обычно забывают."""
    try:
        import playwright                               # noqa: F401
    except Exception:                                   # noqa: BLE001
        return Item("playwright", False,
                    what="пакет playwright не установлен — проверки dom "
                         "и screenshot не выполнятся",
                    fix="pip install playwright && python -m playwright "
                        "install chromium",
                    blocking=False)
    try:
        found = any(p.name.startswith("chromium")
                    for p in _browsers_dir().iterdir())
    except OSError:
        found = False
    return Item("playwright", found,
                what="chromium не установлен — проверки dom и screenshot "
                     "не выполнятся",
                fix="python -m playwright install chromium",
                blocking=False)


def _key() -> Item:
    """Ключ нужен только судье на API. По умолчанию всё идёт по подписке,
    поэтому это НЕ блокирующий пункт: называть установку негодной из-за
    ключа, который в рабочем режиме не нужен, — ложная тревога."""
    from .vault import anthropic_key

    needed = "api" in (cfg.llm_backend_judge or cfg.llm_backend)
    return Item("key", bool(anthropic_key()) or not needed,
                what="нет ANTHROPIC_API_KEY, а судья настроен на API "
                     "(LLM_BACKEND_JUDGE=api)",
                fix="python scripts/vault_cli.py add ANTHROPIC_API_KEY — "
                    "или переведи судью на подписку (LLM_BACKEND_JUDGE=cli)",
                blocking=needed)


CHECKS = (_claude, _git, _playwright, _key)


def check() -> list[Item]:
    """Все проверки. Каждая обязана вернуть Item, а не упасть: диагностика,
    которая сама падает, бесполезнее отсутствующей."""
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:                          # noqa: BLE001
            log.exception("проверка готовности %s не отработала", fn.__name__)
            out.append(Item(fn.__name__.strip("_"), False,
                            what=f"проверку не удалось выполнить: {e}",
                            fix="посмотри лог", blocking=False))
    return out


def problems(items=None) -> list[Item]:
    return [i for i in (items if items is not None else check()) if not i.ok]


def ready(items=None) -> bool:
    """Можно ли работать автономно. Конъюнкция, а не балл: одного
    отсутствующего git достаточно, чтобы приёмка начала врать."""
    return not any(i.blocking for i in problems(items))


def report(items=None) -> str:
    """Человеку — списком, у каждого пункта своё «что делать»."""
    items = items if items is not None else check()
    bad = [i for i in items if not i.ok]
    if not bad:
        return "установка готова к автономной работе"
    lines = []
    for item in bad:
        mark = "БЛОКИРУЕТ" if item.blocking else "мешает"
        lines.append(f"[{mark}] {item.title}: {item.what}\n    → {item.fix}")
    return "\n".join(lines)
