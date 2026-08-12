"""Даты и деньги: SQLite не хранит tzinfo, а бюджет считается по окну в 24 часа."""
from __future__ import annotations

import datetime as dt

from conftest import make_project, make_tasks

from autopilot.db import Project, Run, Session, spent_today, utcnow


async def test_datetime_roundtrip_is_timezone_aware(db):
    p = await make_project()
    async with Session() as s:
        stored = await s.get(Project, p.id)

    assert stored.updated_at.tzinfo is not None, "из SQLite вернулся naive datetime"
    # главное следствие: арифметика с utcnow() не падает
    assert (utcnow() - stored.updated_at).total_seconds() >= 0


async def test_naive_datetime_is_treated_as_utc(db):
    p = await make_project()
    naive = dt.datetime(2026, 1, 1, 12, 0, 0)
    async with Session() as s:
        proj = await s.get(Project, p.id)
        proj.updated_at = naive
        await s.commit()

    async with Session() as s:
        proj = await s.get(Project, p.id)
    assert proj.updated_at == naive.replace(tzinfo=dt.timezone.utc)


async def test_date_column_roundtrip(db):
    day = dt.date.today() + dt.timedelta(days=3)
    p = await make_project(deadline=day)
    async with Session() as s:
        proj = await s.get(Project, p.id)
    assert proj.deadline == day
    assert (proj.deadline - dt.date.today()).days == 3


async def test_spent_today_uses_24h_window(db):
    p = await make_project()
    (task_id,) = await make_tasks(p.id, 1)

    async with Session() as s:
        s.add(Run(task_id=task_id, cost_usd=2.0))
        s.add(Run(task_id=task_id, cost_usd=3.0))
        old = Run(task_id=task_id, cost_usd=100.0)
        old.started_at = utcnow() - dt.timedelta(hours=30)
        s.add(old)
        await s.commit()

    assert await spent_today() == 5.0
