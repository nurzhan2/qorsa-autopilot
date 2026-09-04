"""Цикл, который стоит на месте, надо останавливать РАНЬШЕ, чем кончатся попытки.

Задача 531 («Веб-админка») четыре раза подряд упала с ОДНИМ И ТЕМ ЖЕ дефектом:
критерий ждал `302`, сервер отдавал `200`. Разбор (`crew.Diagnostician`) со
второй попытки объяснял, что делать иначе, — и всё равно третий и четвёртый
заходы кончились тем же. Причина была не в работе агента вовсе: критерий не мог
пройти НИКОГДА, потому что клиент приёмки шёл за редиректом. Но узнали мы это
руками, потратив четыре сессии по шесть минут и ночь работы заказа.

Механически это видно без всякой модели: **дефект повторился дословно**.
Повторяющийся дефект — это не «агент опять не справился», это «цикл не
сходится», и лечится он не пятой попыткой, а человеком.

Отсюда два вывода, и оба реализованы здесь ДЕТЕРМИНИРОВАННО:

* **застревание** — один и тот же дефект подряд `BREAKER_STAGNATION` раз.
  Порог 3, а не 2, намеренно: разбор включается со ВТОРОЙ попытки, и третий
  заход — первый, который вообще мог им воспользоваться. Остановиться на
  двойке значило бы не дать разбору ни одного шанса;
* **буксование** — разбор второй раз подряд говорит одно и то же. Значит
  советовать ему больше нечего, и следующая попытка будет копией предыдущей.

Чего здесь намеренно НЕТ, хотя есть у прототипа (loop-engineering,
`tools/loop-context`):

* «no-progress» (N провалов подряд без успеха) — это ровно `MAX_ATTEMPTS`,
  который в проекте давно работает. Второй счётчик того же самого
  разошёлся бы с первым в первый же спорный случай;
* потолок токенов — это `limits.py` и суточные бюджеты `db.spent_today()`.
  Дублировать их здесь значит завести третье мнение о том же ресурсе.

Модуль **чистый**: ни базы, ни сети, ни модели. Он получает список попыток
и возвращает решение. Записывает попытки и исполняет решение планировщик.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Насколько похожими считаем два дефекта. Порог взят у прототипа (0.85) и
# проверен на живых парах из проекта 8: переформулировки одного и того же
# дефекта дают 0.9+, разные дефекты — заметно ниже 0.8.
SIMILAR = 0.85

TRIGGERS = ("ok", "stagnation", "frustration")


# ---------- подпись дефекта ----------

# Что выкидываем из текста дефекта, прежде чем сравнивать. Каждая строка —
# это то, что МЕНЯЕТСЯ от прогона к прогону, не меняя сути дефекта.
_VOLATILE = (
    # ISO-время и время в скобках
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\b"), "<время>"),
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), "<время>"),
    # шестнадцатеричные адреса и хеши
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<адрес>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<хеш>"),
    # длительности и объёмы: «за 3.42s», «12.5 MB», «в 1834 мс»
    (re.compile(r"\b\d+([.,]\d+)?\s*(s|ms|sec|мс|с|мин|min|h|ч)\b", re.I), "<длительность>"),
    (re.compile(r"\b\d+([.,]\d+)?\s*(b|kb|mb|gb|кб|мб|гб)\b", re.I), "<объём>"),
    # номер строки и колонки в трассировке
    (re.compile(r":\d+(:\d+)?(?=[\s,)\]]|$)"), ":<строка>"),
)

# Абсолютный путь схлопываем до имени файла: один и тот же дефект в разных
# установках и во временных каталогах pytest выглядел бы разными дефектами.
#
# **ТОЛЬКО АБСОЛЮТНЫЙ**, и это правка по живому промаху. Первая версия
# требовала лишь двух сегментов через слэш — и попадала внутрь URL:
# `http://localhost:8000/admin/` схлопывалось до пустого хвоста, а значит
# `GET .../admin/ -> 200, ожидалось 302` и `GET .../orders/ -> 200,
# ожидалось 302` давали ОДНУ подпись. Два разных дефекта объявлялись
# застреванием — то есть механизм, написанный ради автономности, снимал
# с работы движущуюся задачу.
#
# Теперь путь опознаётся по началу: буква диска или ведущий слэш. Адрес
# в URL остаётся целиком, потому что он и есть суть http-дефекта.
_PATH = re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/]|[\\/])[^\s:*?\"<>|]{2,}")


def signature(defects) -> str:
    """Устойчивая подпись провала: то, что должно совпасть, чтобы считать
    два провала «одним и тем же».

    **Числа НЕ вычищаются подряд, и это расхождение с прототипом.**
    `tools/loop-context` заменяет на `#` любое число — для стека вызовов это
    верно, а для нашей приёмки губительно: `GET /admin/ -> 200, ожидалось 302`
    и `GET /admin/ -> 404, ожидалось 200` превратились бы в одну подпись,
    и два РАЗНЫХ дефекта считались бы застреванием. У нас число в дефекте
    чаще всего и есть его суть — код ответа, число упавших тестов. Поэтому
    вычищается только заведомо изменчивое: время, адреса, хеши, длительности,
    объёмы, номера строк и абсолютные пути.
    """
    if isinstance(defects, str):
        parts = [defects]
    else:
        parts = [str(d) for d in (defects or []) if str(d).strip()]
    text = " | ".join(parts).strip()
    if not text:
        return ""

    text = _PATH.sub(lambda m: m.group(0).replace("\\", "/").rsplit("/", 1)[-1], text)
    for pattern, repl in _VOLATILE:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _trigrams(text: str) -> set[str]:
    padded = f"  {text}  "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / (len(a) + len(b) - common)


def similarity(a: str, b: str) -> float:
    """Похожесть двух подписей: ЛУЧШЕЕ из двух дешёвых мер.

    Тот же приём, что у `brief.same_item()`, и по той же причине — одна мера
    промахивается предсказуемым образом, две вместе промахиваются реже:

    * **по триграммам** (как у прототипа) — ловит мелкие правки внутри
      той же фразы: сменившийся путь, другой номер, лишнее слово;
    * **по множеству слов** — ловит ПЕРЕСТАНОВКУ. Судья регулярно пишет
      то же самое задом наперёд («критерий не выполнен: подпись не
      проверяется» против «подпись не проверяется — критерий не выполнен»),
      и посимвольная мера даёт на этом 0.81, то есть «разные дефекты».

    Чего пара не умеет и не притворяется: **полный пересказ другими словами
    не опознаётся**. «ожидалось 302, получено 200» и «сервер отдал страницу
    вместо редиректа» — для этой функции разные дефекты. Ошибаться она
    предпочитает в эту сторону: не заметить застревание значит потратить
    ещё одну попытку, а увидеть его там, где его нет, значит остановить
    работу, которая шла.
    """
    if a == b:
        return 1.0
    by_char = _jaccard(_trigrams(a), _trigrams(b))
    by_word = _jaccard(set(_WORD.findall(a)), set(_WORD.findall(b)))
    return max(by_char, by_word)


# ---------- попытки и решение ----------

@dataclass
class Attempt:
    """Одна неудачная попытка. Пишется планировщиком в `Task.attempt_log`."""

    n: int = 0                       # номер попытки, с единицы
    lane: str = "build"              # где провалилась: сборка или приёмка
    defects: list[str] = field(default_factory=list)
    sig: str = ""                    # подпись, посчитанная при записи
    note: str = ""                   # что посоветовал разбор перед этим заходом
    at: str = ""                     # ISO-время, для журнала и для глаз

    @classmethod
    def make(cls, n: int, lane: str, defects, note: str = "", at: str = "") -> "Attempt":
        items = [str(d) for d in (defects or []) if str(d).strip()]
        return cls(n=n, lane=lane, defects=items, sig=signature(items),
                   note=(note or "").strip(), at=at)

    def as_dict(self) -> dict:
        return {"n": self.n, "lane": self.lane, "defects": self.defects,
                "sig": self.sig, "note": self.note, "at": self.at}

    @classmethod
    def from_dict(cls, raw) -> "Attempt | None":
        if not isinstance(raw, dict):
            return None
        defects = raw.get("defects")
        if not isinstance(defects, list):
            defects = [str(defects)] if defects else []
        return cls(n=int(raw.get("n") or 0), lane=str(raw.get("lane") or "build"),
                   defects=[str(d) for d in defects],
                   sig=str(raw.get("sig") or "") or signature(defects),
                   note=str(raw.get("note") or ""), at=str(raw.get("at") or ""))


def load(raw) -> list[Attempt]:
    """Журнал попыток из JSON-колонки. Мусор молча пропускается: колонку
    правят руками и скриптами, и падать из-за одной кривой записи нельзя."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        a = Attempt.from_dict(item)
        if a is not None:
            out.append(a)
    return out


@dataclass
class Decision:
    """Что делать с задачей после очередного провала."""

    trigger: str = "ok"
    stop: bool = False
    reason: str = ""
    repeats: int = 0

    def __bool__(self) -> bool:      # `if decision:` — «цикл надо остановить»
        return self.stop


_NUM = re.compile(r"\d+")


def same_defect(a: str, b: str, threshold: float = SIMILAR) -> bool:
    """Один ли это дефект. Похожести МАЛО — числа обязаны совпасть.

    Живой промах, ради которого добавлено второе условие. Дефект приёмки —
    это хвост вывода команды, и он длинный:

        FAILED tests/test_cart.py::test_total ... 6 failed, 12 passed in 4.2s
        FAILED tests/test_cart.py::test_total ... 5 failed, 13 passed in 4.4s

    Одна изменившаяся цифра в трёхстах символах даёт похожесть 0.99, то есть
    «тот же дефект». А это ПРОГРЕСС: агент чинит по одному тесту за заход,
    и объявить его застрявшим — снять с работы то, что как раз работает.

    Волатильные числа к этому моменту уже вычищены нормализацией (время,
    длительности, объёмы, номера строк). Всё, что осталось, — суть дефекта:
    код ответа, число упавших тестов, код возврата. Изменилось хоть одно —
    что-то сдвинулось.
    """
    if not a or not b:
        return False
    if _NUM.findall(a) != _NUM.findall(b):
        return False
    return similarity(a, b) >= threshold


def _tail_same(values: list[str], threshold: float = SIMILAR,
               strict: bool = True) -> int:
    """Сколько последних значений подряд совпадают с самым последним.

    `strict` — сверять и числа (для дефектов). Для советов разбора числа
    несущественны: там сравнивается человеческий текст.
    """
    if not values:
        return 0
    last = values[-1]
    if not last:
        return 0
    match = same_defect if strict else (
        lambda x, y: similarity(x, y) >= threshold)
    same = 0
    for value in reversed(values):
        if value and match(value, last):
            same += 1
        else:
            break
    return same


def check(attempts: list[Attempt], *, stagnation: int = 3,
          frustration: int = 2) -> Decision:
    """Сходится ли цикл. Возвращает решение, а не меняет статус.

    Проверки идут от самой содержательной к самой формальной, как у прототипа:
    сперва «дефект тот же» (это точный диагноз и по нему понятно, что делать),
    потом «совет тот же» (это уже про то, что советовать больше нечего).
    """
    sigs = [a.sig for a in attempts]
    same = _tail_same(sigs)
    if stagnation > 0 and same >= stagnation:
        return Decision(
            trigger="stagnation", stop=True, repeats=same,
            reason=(f"один и тот же дефект {same} раза подряд — цикл не сходится. "
                    f"Проверь, ВЫПОЛНИМ ли критерий вообще: повторяющийся дефект "
                    f"чаще означает неверный критерий, чем плохую работу агента. "
                    f"Дефект: {(attempts[-1].defects or ['—'])[0][:200]}"))

    notes = [a.note for a in attempts if a.note]
    if frustration > 0 and len(notes) >= frustration:
        repeat = _tail_same(notes, strict=False)
        if repeat >= frustration:
            return Decision(
                trigger="frustration", stop=True, repeats=repeat,
                reason=(f"разбор {repeat} раза подряд советует одно и то же, "
                        f"а дефект остаётся. Советовать агенту больше нечего: "
                        f"«{notes[-1][:200]}»"))

    return Decision(trigger="ok", stop=False, reason="цикл ещё сходится")


# ---------- что уже пробовали ----------

def tried_block(attempts: list[Attempt], limit: int = 5) -> str:
    """Блок промпта «что уже пробовал — не повторяй».

    Раньше агент получал только дефекты ПОСЛЕДНЕЙ итерации и не знал, что
    три захода назад он ломился ровно туда же. Блок собирается кодом из
    журнала попыток и не стоит ни одного вызова модели — в отличие от
    разбора (`crew.Diagnostician`), который платит окном подписки.

    Одинаковые дефекты схлопываются в один пункт со счётчиком: пять
    дословных повторов в промпте — это шум, который вытесняет полезное.
    """
    if not attempts:
        return ""

    groups: list[tuple[str, int]] = []
    for a in attempts:
        text = (a.defects or ["—"])[0].strip()
        if not text:
            continue
        if groups and same_defect(signature(text), signature(groups[-1][0])):
            groups[-1] = (groups[-1][0], groups[-1][1] + 1)
        else:
            groups.append((text, 1))
    if not groups:
        return ""

    lines = ["# ЧТО УЖЕ ПРОБОВАЛ — НЕ ПОВТОРЯЙ",
             f"Заходов на эту задачу: {len(attempts)}. Чем они кончались:"]
    for text, count in groups[-limit:]:
        suffix = f" (повторилось {count} раза подряд)" if count > 1 else ""
        lines.append(f"- {text[:300]}{suffix}")
    if any(count > 1 for _, count in groups):
        lines.append("- Один и тот же дефект повторяется. Значит прошлый подход "
                     "не работает — меняй подход, а не детали.")
    lines.append("- Не обходи проверку и не ослабляй критерий: он и есть приёмка.")
    return "\n".join(lines)
