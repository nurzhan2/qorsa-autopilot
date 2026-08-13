"""Реестр типов приёмочных проверок — единственный источник правды.

Из этого файла читают оба конца:

* `planner.py` — что он вправе выдать в acceptance и что даёт класс `auto`;
* `verifier.py` — что он обязан уметь исполнить.

Разрыв между этими списками — самая опасная поломка проекта из возможных.
Так и было: планировщик выдавал `dom`, `file_exists` и `screenshot`, а
верификатор знал только `shell`, `http` и `llm`. Задача с одними
dom-проверками проходила приёмку как «нет применимых критериев», то есть
успешно и вообще без проверок.

Поэтому:

1. типы описаны здесь и только здесь;
2. `verifier.HANDLERS` обязан покрывать `REGISTRY` целиком — это проверяется
   тестом, который падает при расхождении;
3. проверка, которую нельзя исполнить, считается **провалом**, а не поводом
   пропустить приёмку.
"""
from __future__ import annotations

import dataclasses as dc


@dc.dataclass(frozen=True)
class CheckSpec:
    kind: str
    # без этих полей проверка не запустится — значит это не проверка,
    # а обещание, и код её выбрасывает
    required: tuple[str, ...]
    # даёт ли право называться auto: вердикт выносит код, без модели и человека
    deterministic: bool
    # вердикт выносит модель — подсказка, не приговор
    assisted: bool
    # как объяснить тип модели в промпте планировщика
    template: str
    hint: str


REGISTRY: dict[str, CheckSpec] = {
    "shell": CheckSpec(
        "shell", ("cmd",), True, False,
        '{"type":"shell","cmd":"npm run build"}',
        "команда в каталоге проекта, код возврата 0"),
    "http": CheckSpec(
        "http", ("url",), True, False,
        '{"type":"http","url":"https://site/","expect":200}',
        "HTTP-запрос и ожидаемый статус"),
    "file_exists": CheckSpec(
        "file_exists", ("path",), True, False,
        '{"type":"file_exists","path":"wp-content/themes/shop/style.css"}',
        "файл существует; путь ТОЛЬКО внутри каталога проекта"),
    "dom": CheckSpec(
        "dom", ("url", "selector"), True, False,
        '{"type":"dom","url":"https://site/shop/","selector":".products .product","min_count":1}',
        "страница открывается в headless-браузере (JS выполняется) "
        "и селектор найден не меньше min_count раз"),
    "screenshot": CheckSpec(
        "screenshot", ("url", "criteria"), False, True,
        '{"type":"screenshot","url":"https://site/","criteria":"каталог в три колонки, '
        'не разъезжается на мобильном"}',
        "снимки на 375/768/1440 оценивает модель по твоему описанию"),
    "llm": CheckSpec(
        "llm", ("criteria",), False, True,
        '{"type":"llm","criteria":"в ответе API есть поле price"}',
        "модель судит по дифу изменений"),
    "human": CheckSpec(
        "human", ("criteria",), False, False,
        '{"type":"human","criteria":"вёрстка совпадает с макетом"}',
        "проверить может только человек глазами"),
}

KNOWN = tuple(REGISTRY)
DETERMINISTIC = tuple(k for k, v in REGISTRY.items() if v.deterministic)
ASSISTED = tuple(k for k, v in REGISTRY.items() if v.assisted)
# «human» не проверка, а признание, что машине тут делать нечего
HUMAN_ONLY = tuple(k for k, v in REGISTRY.items()
                   if not v.deterministic and not v.assisted)

# Ширины окна для screenshot. 375 — телефон, 768 — планшет, 1440 — десктоп.
VIEWPORTS = ((375, 812), (768, 1024), (1440, 900))

DEFAULT_TIMEOUT_SEC = 30


def spec(kind: str) -> CheckSpec | None:
    return REGISTRY.get(str(kind or ""))


def is_runnable(check: dict) -> bool:
    """Можно ли эту проверку вообще выполнить.

    `human` сюда не входит: он честно говорит, что машина не судья.
    """
    s = spec(check.get("type") if isinstance(check, dict) else None)
    if s is None or not (s.deterministic or s.assisted):
        return False
    return all(str(check.get(f) or "").strip() for f in s.required)


def valid_checks(acceptance) -> list[dict]:
    """Оставляет проверки известного типа с заполненными обязательными полями.

    Проверка без `cmd` у `shell` не запустится никогда — держать её в плане
    значит обманывать себя насчёт приёмки.
    """
    out = []
    for check in acceptance or []:
        if not isinstance(check, dict):
            continue
        s = spec(check.get("type"))
        if s is None:
            continue
        if not all(str(check.get(f) or "").strip() for f in s.required):
            continue
        out.append(check)
    return out


def prompt_reference() -> str:
    """Описание типов для промпта планировщика — из того же реестра.

    Иначе промпт и код разъезжаются: в промпте появляется тип, которого
    верификатор не знает, и мы возвращаемся ровно к той дыре, ради которой
    этот модуль и заведён.
    """
    lines = []
    for s in REGISTRY.values():
        lines.append(f"     {s.kind:12s} {s.template}")
        lines.append(f"                  — {s.hint}")
    return "\n".join(lines)


# ---------- критерии, которые проходят на пустом проекте ----------

# Селекторы, которые есть на любой странице: их наличие ничего не доказывает
GENERIC_SELECTORS = {
    "html", "body", "head", "title", "div", "a", "p", "span", "img",
    "header", "footer", "main", "nav", "h1", "*",
}


def suspicious(check: dict) -> str | None:
    """Критерий, который пройдёт, даже если задачу не делать.

    Главное правило приёмки: критерий обязан ПАДАТЬ на состоянии «эта задача
    не выполнена, всё остальное на месте». Проверка «главная отдаёт 200»
    проходит на пустом WordPress без единого плагина — для задачи «установить
    WooCommerce» она бесполезна.

    Возвращает причину подозрения или None. Это эвристика, а не приговор:
    показываем человеку, решает человек.
    """
    if not isinstance(check, dict):
        return None
    kind = str(check.get("type") or "")

    if kind == "http":
        url = str(check.get("url") or "")
        expect = int(check.get("expect", 200) or 200)
        path = url.split("://", 1)[-1]
        path = path[path.find("/"):] if "/" in path else "/"
        if expect == 200 and path.strip("/") in ("", "index.php", "index.html"):
            return "корень сайта отдаёт 200 и без этой задачи"

    if kind == "dom":
        selector = str(check.get("selector") or "").strip()
        head = selector.split(",")[0].strip().lower()
        if head in GENERIC_SELECTORS:
            return f"селектор {selector!r} есть на любой странице"

    if kind == "shell":
        cmd = str(check.get("cmd") or "").lower()
        if cmd.startswith(("echo ", "true", "ls ", "pwd")):
            return "команда ничего не проверяет"

    return None
