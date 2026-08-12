"""Маршрутизация и агрегация: кому уходит сообщение и сколько их."""
from __future__ import annotations

import datetime as dt

from conftest import make_access, make_project
from sqlalchemy import func, select

from autopilot.communicator import (TO_CLIENT, TO_MANAGER, TO_OWNER, TOPIC_COMMERCIAL,
                                    Communicator, in_quiet_hours, route, strictest, topic)
from autopilot.config import cfg
from autopilot.db import AccessItem, Message, Session, Task, utcnow


class Spy:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    def to(self, chat_id: str) -> list[str]:
        return [t for c, t in self.sent if c == chat_id]


async def test_routing_manager(db, monkeypatch):
    """Вопрос про деньги уходит человеку, клиенту при этом — ничего.

    Менеджер и владелец — один аккаунт, поэтому адресат коммерции —
    владелец. Тема при этом остаётся коммерческой: именно на ней держится
    молчание бота в группе.
    """
    monkeypatch.setattr(cfg, "manager_separate", False)
    monkeypatch.setattr(cfg, "tg_owner", "owner-chat")
    p = await make_project(tg_chat_id="client-chat")
    spy = Spy()
    comm = Communicator(send_fn=spy)

    assert topic("сколько будет стоить доработка?") == TOPIC_COMMERCIAL
    assert route("сколько будет стоить доработка?") == TO_OWNER

    m = await comm.incoming(p, "сколько будет стоить доработка?")
    assert m.route == TO_OWNER

    assert await comm.pump_once() == 1
    assert spy.to("client-chat") == [], "клиенту ушло то, на что бот отвечать не вправе"
    assert len(spy.to("owner-chat")) == 1
    assert "сколько будет стоить" in spy.to("owner-chat")[0]


async def test_routing_client_auto(db):
    """Запрос доступа по чеклисту бот отправляет клиенту сам."""
    p = await make_project(tg_chat_id="client-chat")
    await make_access(p.id, "FTP хостинга", "ftp", "needed")
    spy = Spy()
    comm = Communicator(send_fn=spy)

    text = "Чтобы начать, нужен доступ к FTP: хост, логин, пароль."
    assert route(text) == TO_CLIENT

    async with Session() as s:
        items = (await s.execute(select(AccessItem))).scalars().all()
    m = await comm.remind_access(p, items)
    assert m is not None
    assert m.route == TO_CLIENT
    assert m.status == "scheduled"

    async with Session() as s:
        stored = await s.get(Message, m.id)
        stored.send_after = utcnow() - dt.timedelta(seconds=1)   # приблизили срок
        await s.commit()

    assert await comm.pump_once() == 1
    assert len(spy.to("client-chat")) == 1
    assert "FTP хостинга" in spy.to("client-chat")[0]


async def test_routing_owner(db, monkeypatch):
    """Сломанный доступ и нераспознаваемое — владельцу, не менеджеру и не клиенту."""
    monkeypatch.setattr(cfg, "tg_owner", "owner-chat")
    p = await make_project(tg_chat_id="client-chat")
    spy = Spy()
    comm = Communicator(send_fn=spy)

    assert route("FTP не пускает, пароль не подходит") == TO_OWNER
    assert route("👍") == TO_OWNER            # ни одной буквы — человеку разбираться

    await comm.incoming(p, "FTP не пускает, пароль не подходит")
    assert await comm.pump_once() == 1
    assert len(spy.to("owner-chat")) == 1
    assert spy.to("client-chat") == []


async def test_doubt_goes_to_human(db):
    """Правило по умолчанию: непонятное, но осмысленное — человеку, не боту."""
    assert topic("ок, давайте так и сделаем") == TOPIC_COMMERCIAL
    assert route("ок, давайте так и сделаем") == TO_OWNER
    assert route("а ещё бы логотип поменять") == TO_OWNER


async def test_aggregation(db):
    """10 закрытых задач в одном окне -> одно сообщение с накопленным списком."""
    p = await make_project()
    comm = Communicator(send_fn=Spy())

    for i in range(10):
        await comm.on_task_done(Task(id=i + 1, project_id=p.id, title=f"шаг {i + 1}"), p)

    async with Session() as s:
        n = (await s.execute(
            select(func.count()).select_from(Message)
            .where(Message.kind == "task_done"))).scalar_one()
        m = (await s.execute(select(Message).where(Message.kind == "task_done"))).scalars().one()

    assert n == 1, f"вместо одного отчёта создано {n}"
    assert m.route == TO_CLIENT
    for i in range(10):
        assert f"шаг {i + 1}" in m.text, "список накопился не полностью"


async def test_aggregate_window_respected(db):
    """После отправки следующий отчёт не раньше окна агрегации."""
    p = await make_project()
    comm = Communicator(send_fn=Spy())

    await comm.on_task_done(Task(id=1, project_id=p.id, title="шаг 1"), p)
    async with Session() as s:
        m = (await s.execute(select(Message))).scalars().one()
        m.status, m.sent_at = "sent", utcnow()
        await s.commit()

    await comm.on_task_done(Task(id=2, project_id=p.id, title="шаг 2"), p)
    async with Session() as s:
        fresh = (await s.execute(
            select(Message).where(Message.sent_at.is_(None)))).scalars().one()
    delay_min = (fresh.send_after - utcnow()).total_seconds() / 60
    assert delay_min >= cfg.aggregate_window_min - 1, f"следующий отчёт через {delay_min:.0f} мин"


async def test_stage_done_flushes_immediately(db):
    p = await make_project()
    comm = Communicator(send_fn=Spy())
    await comm.on_task_done(Task(id=1, project_id=p.id, title="шаг 1"), p)
    await comm.on_stage_done(p)

    async with Session() as s:
        m = (await s.execute(select(Message))).scalars().one()
    assert m.send_after <= utcnow()


async def test_access_reminder_once_per_day(db):
    """За сутки уходит ровно одно напоминание, а не по пункту на каждое."""
    p = await make_project()
    for name, kind in (("FTP", "ftp"), ("Панель", "hosting_panel"), ("Домен", "domain")):
        await make_access(p.id, name, kind, "needed")
    comm = Communicator(send_fn=Spy())

    async with Session() as s:
        items = (await s.execute(select(AccessItem))).scalars().all()

    first = await comm.remind_access(p, items)
    assert first is not None
    for _ in range(5):
        assert await comm.remind_access(p, items) is None, "напомнил повторно в тех же сутках"

    async with Session() as s:
        msgs = (await s.execute(
            select(Message).where(Message.kind == "access_reminder"))).scalars().all()
    assert len(msgs) == 1
    for name in ("FTP", "Панель", "Домен"):
        assert name in msgs[0].text, "напоминание должно перечислять все пункты сразу"

    # сутки прошли — можно снова
    async with Session() as s:
        stored = await s.get(Message, msgs[0].id)
        stored.created_at = utcnow() - dt.timedelta(hours=cfg.access_reminder_h + 1)
        await s.commit()
    assert await comm.remind_access(p, items) is not None


async def test_strictest():
    assert strictest(TO_CLIENT, TO_MANAGER) == TO_MANAGER
    assert strictest(TO_CLIENT, TO_OWNER) == TO_OWNER
    assert strictest(TO_CLIENT, TO_CLIENT) == TO_CLIENT


async def test_quiet_hours_holds_client_only(db, monkeypatch):
    """Ночью молчим клиенту, но не своим."""
    monkeypatch.setattr(cfg, "tg_owner", "owner-chat")
    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 24)      # тишина круглосуточно
    p = await make_project(tg_chat_id="client-chat")
    spy = Spy()
    comm = Communicator(send_fn=spy)

    m = await comm.draft(p, "выложил превью")             # клиенту
    await comm.draft(p, "клиент просит скидку")            # менеджеру
    async with Session() as s:
        stored = await s.get(Message, m.id)
        stored.send_after = utcnow() - dt.timedelta(seconds=1)
        await s.commit()

    assert await comm.pump_once() == 1
    assert spy.to("client-chat") == []
    assert len(spy.to("owner-chat")) == 1


def test_quiet_hours_window(monkeypatch):
    monkeypatch.setattr(cfg, "quiet_start", 23)
    monkeypatch.setattr(cfg, "quiet_end", 9)
    assert in_quiet_hours(dt.datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours(dt.datetime(2026, 1, 1, 3, 0))
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 12, 0))

    monkeypatch.setattr(cfg, "quiet_start", 0)
    monkeypatch.setattr(cfg, "quiet_end", 0)
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 3, 0))
