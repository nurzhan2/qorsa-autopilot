"""Контракт транспорта и нормализованная форма входящего сообщения."""
from __future__ import annotations

import asyncio
import dataclasses as dc
import datetime as dt
import logging
import random
from typing import AsyncIterator, Protocol, runtime_checkable

from ..db import Session, TransportState, utcnow

log = logging.getLogger("transport")


@dc.dataclass(frozen=True)
class InboundMessage:
    """Общая форма для Telegram и MAX.

    Всё, что специфично для мессенджера, остаётся в `raw`. Логика выше
    транспорта обязана обходиться полями этого класса — иначе через месяц
    в маршрутизации заведётся `if transport == "telegram"`, и смысл абстракции
    пропадёт.
    """
    transport: str
    chat_id: str
    message_id: str
    text: str = ""
    date: dt.datetime = dc.field(default_factory=utcnow)
    sender_id: str | None = None
    handle: str | None = None            # @username или телефон отправителя
    has_media: bool = False
    media_kind: str | None = None        # photo|video|document|voice|audio|sticker|...
    reply_to: str | None = None
    reply_to_sender_id: str | None = None
    sender_name: str = ""
    # групповые чаты: по названию опознаём проект, по типу — можно ли молчать
    chat_title: str = ""
    chat_type: str = "private"       # private | group | supergroup | dialog | chat
    mentions_bot: bool = False
    edited: bool = False
    deleted: bool = False
    direction: str = "in"
    # Telegram Business: от чьего имени отвечать. В MAX аналога нет — None
    connection_id: str | None = None
    # позиция в потоке апдейтов, подтверждается ПОСЛЕ записи в БД
    cursor: str | None = None
    raw: dict = dc.field(default_factory=dict)


@runtime_checkable
class Transport(Protocol):
    name: str

    def poll(self) -> AsyncIterator[InboundMessage]: ...

    async def send(self, chat_ref: str, text: str) -> str: ...

    async def resolve_chat(self, handle: str) -> str | None: ...

    def supports_impersonation(self) -> bool: ...


# ---------- offset в БД: рестарт не теряет и не дублирует ----------

async def load_offset(transport: str) -> str | None:
    async with Session() as s:
        row = await s.get(TransportState, transport)
        return row.offset if row else None


async def save_offset(transport: str, value: str | None) -> None:
    async with Session() as s:
        row = await s.get(TransportState, transport)
        if row is None:
            row = TransportState(transport=transport)
            s.add(row)
        row.offset = None if value is None else str(value)
        row.updated_at = utcnow()
        await s.commit()


class Backoff:
    """Экспоненциальная пауза с джиттером. Без джиттера все транспорты
    после общего сбоя сети ломятся обратно одновременно."""

    def __init__(self, start: float = 1.0, cap: float = 60.0):
        self.start = start
        self.cap = cap
        self.value = start

    def reset(self) -> None:
        self.value = self.start

    async def sleep(self, forced: float | None = None) -> None:
        delay = forced if forced is not None else self.value * random.uniform(0.8, 1.3)
        delay = min(delay, self.cap if forced is None else forced)
        log.info("пауза перед переподключением: %.1fs", delay)
        await asyncio.sleep(delay)
        if forced is None:
            self.value = min(self.value * 2, self.cap)
