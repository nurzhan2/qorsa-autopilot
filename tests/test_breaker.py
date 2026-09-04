"""Детектор застревания: цикл, который стоит на месте, останавливается сам.

Живой случай, ради которого модуль написан, — задача 531: четыре одинаковых
провала подряд, четыре сессии агента и ночь работы заказа впустую. Тесты
проверяют ровно два свойства: одинаковое опознаётся как одинаковое, а разное
разным и остаётся. Второе важнее: ложное срабатывание останавливает работу,
которая на самом деле шла.
"""
from __future__ import annotations

from autopilot import breaker
from autopilot.breaker import Attempt


def fail(n: int, defect: str, note: str = "", lane: str = "verify") -> Attempt:
    return Attempt.make(n, lane, [defect], note=note)


# ---------- подпись ----------

def test_signature_ignores_time_paths_and_durations():
    """Изменчивое между прогонами не должно делать дефект «другим»."""
    a = breaker.signature(["pytest упал: C:/Users/x/tmp/qorsa-1/tests/test_cart.py:14 "
                           "за 3.42s в 2026-08-30T11:02:03Z"])
    b = breaker.signature(["pytest упал: D:/other/tmp/qorsa-9/tests/test_cart.py:99 "
                           "за 7.10s в 2026-09-01T20:15:00Z"])
    assert a == b


def test_signature_keeps_numbers_that_are_the_point():
    """РАСХОЖДЕНИЕ С ПРОТОТИПОМ, и оно намеренное.

    loop-context заменяет любое число на `#`. У нас число в дефекте чаще
    всего и есть его суть: ждали 302, получили 200. Схлопни их — и два
    РАЗНЫХ дефекта объявятся застреванием, а задача уедет к человеку зря.
    """
    got_200 = breaker.signature(["GET /admin/ -> 200, ожидалось 302"])
    got_404 = breaker.signature(["GET /admin/ -> 404, ожидалось 302"])
    assert got_200 != got_404


def test_similarity_survives_word_order():
    """Судья регулярно пишет то же самое задом наперёд. Посимвольная мера
    даёт на этом 0.81 — «разные дефекты»; мера по множеству слов спасает."""
    a = breaker.signature(["критерий не выполнен: подпись вебхука не проверяется"])
    b = breaker.signature(["подпись вебхука не проверяется — критерий не выполнен"])
    assert breaker.similarity(a, b) >= breaker.SIMILAR


def test_similarity_separates_different_defects():
    a = breaker.signature(["GET /admin/ -> 200, ожидалось 302"])
    b = breaker.signature(["pytest tests/test_orders.py: exit=4, файла нет"])
    assert breaker.similarity(a, b) < breaker.SIMILAR


def test_changed_count_is_not_the_same_defect():
    """Число упавших тестов сменилось — что-то сдвинулось, это не буксование."""
    a = breaker.signature(["pytest: 3 failed, ошибка импорта"])
    b = breaker.signature(["pytest: 5 failed, ошибка импорта"])
    assert breaker.similarity(a, b) < breaker.SIMILAR


def test_full_rephrasing_is_a_known_blind_spot():
    """Граница честная, а не недосмотр: пересказ другими словами не ловится.

    Ошибаться модуль предпочитает в эту сторону — лишняя попытка дешевле
    остановленной работы, которая на самом деле шла.
    """
    a = breaker.signature(["ожидалось 302, получено 200"])
    b = breaker.signature(["сервер отдал страницу вместо редиректа"])
    assert breaker.similarity(a, b) < breaker.SIMILAR


# ---------- решение ----------

def test_three_identical_defects_stop_the_loop():
    """Случай 531 дословно: четвёртой попытки быть не должно."""
    same = "GET /admin/ -> 200, ожидалось 302"
    attempts = [fail(1, same), fail(2, same), fail(3, same)]
    d = breaker.check(attempts)
    assert d.stop and d.trigger == "stagnation"
    assert d.repeats == 3
    # причину читает человек — в ней должна стоять главная подсказка
    assert "критерий" in d.reason.lower()


def test_two_identical_defects_still_allow_a_retry():
    """Порог 3, а не 2: разбор включается со второй попытки, и третий заход —
    первый, который вообще мог им воспользоваться."""
    same = "GET /admin/ -> 200, ожидалось 302"
    assert not breaker.check([fail(1, same), fail(2, same)]).stop


def test_different_defects_do_not_stop_the_loop():
    """Разные дефекты — это работа, а не буксование: агент чинит по одному."""
    attempts = [fail(1, "нет файла backend/app/main.py"),
                fail(2, "pytest: 3 failed, ошибка импорта"),
                fail(3, "GET /cart -> 500, ожидалось 401")]
    assert not breaker.check(attempts).stop


def test_streak_resets_after_a_different_defect():
    """Считаем ХВОСТ, а не общее число: два одинаковых, потом другой,
    потом снова тот же — это не три подряд."""
    same = "GET /admin/ -> 200, ожидалось 302"
    attempts = [fail(1, same), fail(2, same),
                fail(3, "pytest: ошибка импорта"), fail(4, same)]
    assert not breaker.check(attempts).stop


def test_repeated_diagnosis_stops_the_loop():
    """Разбор второй раз подряд советует то же самое — советовать нечего."""
    advice = "верни RedirectResponse до рендеринга шаблона"
    attempts = [fail(1, "дефект А", note=advice),
                fail(2, "дефект Б", note=advice)]
    d = breaker.check(attempts)
    assert d.stop and d.trigger == "frustration"


def test_empty_log_is_never_a_stop():
    assert not breaker.check([]).stop
    assert breaker.check([]).trigger == "ok"


def test_attempts_without_defects_do_not_trip_stagnation():
    """Пустой дефект — не подпись. Иначе три пустых записи выглядели бы
    как три одинаковых провала и остановили бы живую задачу."""
    attempts = [fail(1, ""), fail(2, ""), fail(3, "")]
    assert not breaker.check(attempts).stop


# ---------- журнал ----------

def test_log_round_trip_survives_json():
    a = fail(1, "GET /admin/ -> 200, ожидалось 302", note="сделай редирект")
    restored = breaker.load([a.as_dict()])
    assert len(restored) == 1
    assert restored[0].sig == a.sig
    assert restored[0].note == "сделай редирект"


def test_log_ignores_garbage_rows():
    """Колонку правят руками и скриптами. Одна кривая запись не должна
    ронять разбор всей задачи."""
    assert breaker.load(["мусор", None, 42]) == []
    assert breaker.load("вообще не список") == []


# ---------- блок для промпта ----------

def test_tried_block_collapses_repeats():
    same = "GET /admin/ -> 200, ожидалось 302"
    text = breaker.tried_block([fail(1, same), fail(2, same), fail(3, same)])
    assert text.count("GET /admin/") == 1          # схлопнуто, а не трижды
    assert "повторилось 3" in text
    assert "меняй подход" in text


def test_tried_block_lists_distinct_attempts():
    text = breaker.tried_block([fail(1, "нет файла main.py"),
                                fail(2, "pytest: ошибка импорта")])
    assert "main.py" in text and "импорта" in text


def test_tried_block_is_empty_without_attempts():
    assert breaker.tried_block([]) == ""


def test_tried_block_never_offers_to_weaken_the_check():
    """Промпт агенту не должен и намёком разрешать обойти критерий."""
    text = breaker.tried_block([fail(1, "GET /admin/ -> 200, ожидалось 302")])
    assert "не ослабляй критерий" in text
