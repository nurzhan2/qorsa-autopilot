"""Гейты сообщений: красное не уходит клиенту само."""
from __future__ import annotations

import datetime as dt

from conftest import make_project
from sqlalchemy import select

from autopilot.communicator import Communicator, classify, in_quiet_hours, strictest
from autopilot.config import cfg
from autopilot.db import Message, Session, Task, utcnow


class Spy:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


async def test_gates(db):
    """Сообщение со словом «скидка» получает gate=red и не уходит автоматически."""
    p = await make_project()
    spy = Spy()
    comm = Communicator(send_fn=spy)

    m = await comm.draft(p, "Могу сделать скидку 10% на вторую страницу")
    assert m.gate == "red"
    assert m.status == "draft"
    assert m.send_after is None

    assert await comm.pump_once() == 0
    assert spy.sent == []

    async with Session() as s:
        stored = (await s.execute(select(Message).where(Message.id == m.id))).scalar_one()
    assert stored.status == "draft"
    assert stored.sent_at is None


async def test_explicit_green_does_not_override_red(db):
    """draft(gate="green") не должен протаскивать красный текст мимо гейта."""
    p = await make_project()
    comm = Communicator(send_fn=Spy())

    m = await comm.draft(p, "Готово: добавил блок про скидки.", gate="green")
    assert m.gate == "red"
    assert m.status == "draft"


async def test_on_task_done_is_gated(db):
    """Заголовок задачи попадает в текст — значит должен проходить классификацию."""
    p = await make_project()
    comm = Communicator(send_fn=Spy())
    await comm.on_task_done(Task(id=1, project_id=p.id, title="блок про скидку"), p)

    async with Session() as s:
        m = (await s.execute(select(Message))).scalars().one()
    assert m.gate == "red"
    assert m.status == "draft"


async def test_green_message_is_sent(db):
    """Контроль: зелёное с наступившим сроком реально уходит."""
    p = await make_project()
    spy = Spy()
    comm = Communicator(send_fn=spy)

    m = await comm.draft(p, "Выложил превью на тестовый домен.")
    assert m.gate == "green"
    assert m.status == "scheduled"

    async with Session() as s:
        stored = await s.get(Message, m.id)
        stored.send_after = utcnow() - dt.timedelta(minutes=1)   # приблизили срок
        await s.commit()

    assert await comm.pump_once() == 1
    assert spy.sent == [(p.tg_chat_id, "Выложил превью на тестовый домен.")]


async def test_message_without_chat_is_cancelled(db):
    p = await make_project(tg_chat_id=None)
    spy = Spy()
    comm = Communicator(send_fn=spy)

    m = await comm.draft(p, "Привет, начал работу.")
    async with Session() as s:
        stored = await s.get(Message, m.id)
        stored.send_after = utcnow() - dt.timedelta(minutes=1)
        await s.commit()

    assert await comm.pump_once() == 0
    assert spy.sent == []
    async with Session() as s:
        stored = await s.get(Message, m.id)
    assert stored.status == "cancelled"


def test_classify():
    assert classify("итоговая стоимость 150 000") == "red"
    assert classify("успеем к пятнице") == "red"
    assert classify("подпишем договор?") == "red"          # красное важнее вопроса
    assert classify("какой логотип поставить?") == "yellow"
    assert classify("выложил превью") == "green"


def test_strictest():
    assert strictest("green", "red") == "red"
    assert strictest("green", "yellow") == "yellow"
    assert strictest("green", "green") == "green"


def test_quiet_hours(monkeypatch):
    monkeypatch.setattr(cfg, "quiet_start", 23)
    monkeypatch.setattr(cfg, "quiet_end", 9)
    assert in_quiet_hours(dt.datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours(dt.datetime(2026, 1, 1, 3, 0))
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 12, 0))

    # равные границы выключают тишину совсем
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 3, 0))
