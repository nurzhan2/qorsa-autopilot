"""Байты дочернего процесса — в текст, а не в кракозябры.

Живой случай: дефект задачи 536 в базе — `���⥬�...`
вместо русского текста ошибки flutter. Консоль этой машины — cp866,
а слепой decode(errors="replace") без указания кодировки читал как UTF-8
и стирал не-UTF-8 текст в `�`.
"""
from __future__ import annotations

from autopilot.textenc import decode_console


def test_valid_utf8_wins_without_touching_cp866():
    text = "Собрано успешно: 3 файла"
    assert decode_console(text.encode("utf-8")) == text


def test_cp866_console_message_is_recovered():
    """Живой случай: системное сообщение Windows в cp866."""
    text = "Система не может найти указанный путь."
    encoded = text.encode("cp866")
    # слепой UTF-8-decode на этих байтах даёт кракозябры
    blind = encoded.decode("utf-8", errors="replace")
    assert blind.count("�") > 0, "тест ничего не проверяет: байты валидны как UTF-8"
    assert decode_console(encoded) == text


def test_empty_and_none_are_empty_string():
    assert decode_console(b"") == ""
    assert decode_console(None) == ""


def test_ascii_only_output_unaffected():
    assert decode_console(b"exit=1\nOK\n") == "exit=1\nOK\n"
