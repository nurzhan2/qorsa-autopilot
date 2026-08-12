"""Транспорты мессенджеров.

Всё, что выше транспорта — перехват секретов, маршрутизация, агрегация,
привязка к проекту — работает только с `InboundMessage` и не знает,
из какого мессенджера сообщение пришло.
"""
from .base import InboundMessage, Transport, load_offset, save_offset

__all__ = ["InboundMessage", "Transport", "load_offset", "save_offset"]
