"""Наполняет БД пятью демо-проектами, чтобы посмотреть на планировщик вживую.

Ни таблицы, ни Telegram не нужны. Запускать так:

    python scripts/seed_demo.py
    DRY_RUN=1 python -m autopilot.main

DRY_RUN=1 подменяет Claude Code и судью заглушками — демо ничего не тратит.
Скрипт идемпотентен: он сносит только свои строки (title начинается с "DEMO:").
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:                     # под pytest stdout подменён и reconfigure может не быть
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import delete, select                       # noqa: E402

from autopilot.db import (AccessItem, ChatMessage, Message, Project, ProjectChat,  # noqa: E402
                          Run, Session, Task, init_db)

PREFIX = "DEMO:"
TODAY = dt.date.today()

# (заголовок, клиент, приоритет, дедлайн, статус, build-задач, chat-задач,
#  готов к работе, доступы: список (имя, вид, статус))
VERIFIED = [("FTP хостинга", "ftp", "verified"), ("Репозиторий", "git", "verified")]
DEMO = [
    (f"{PREFIX} лендинг под запуск", "Айгерим", 1, TODAY - dt.timedelta(days=1),
     "briefing", 40, 2, True, VERIFIED),
    (f"{PREFIX} интернет-магазин", "Ерлан", 1, TODAY + dt.timedelta(days=2),
     "briefing", 12, 2, True, VERIFIED),
    (f"{PREFIX} корпоративный сайт", "Дана", 2, TODAY + dt.timedelta(days=10),
     "briefing", 12, 2, True, VERIFIED),
    # доступы не пришли: проект обязан встать в blocked_access и не занимать build-слот
    (f"{PREFIX} бот для доставки", "Тимур", 2, None,
     "new", 12, 2, True, [("Панель хостинга", "hosting_panel", "requested"),
                          ("API-ключ платёжки", "api_key", "needed")]),
    # менеджер ещё не поставил галочку «Готов к работе»
    (f"{PREFIX} правки в старом сайте", "Асель", 3, TODAY + dt.timedelta(days=30),
     "briefing", 12, 2, False, VERIFIED),
]


async def wipe(s) -> int:
    ids = (await s.execute(select(Project.id).where(Project.title.like(f"{PREFIX}%")))).scalars().all()
    if not ids:
        return 0
    task_ids = (await s.execute(select(Task.id).where(Task.project_id.in_(ids)))).scalars().all()
    if task_ids:
        await s.execute(delete(Run).where(Run.task_id.in_(task_ids)))
    await s.execute(delete(AccessItem).where(AccessItem.project_id.in_(ids)))
    await s.execute(delete(ChatMessage).where(ChatMessage.project_id.in_(ids)))
    await s.execute(delete(ProjectChat).where(ProjectChat.project_id.in_(ids)))
    await s.execute(delete(Message).where(Message.project_id.in_(ids)))
    await s.execute(delete(Task).where(Task.project_id.in_(ids)))
    await s.execute(delete(Project).where(Project.id.in_(ids)))
    return len(ids)


async def main() -> None:
    await init_db()
    async with Session() as s:
        killed = await wipe(s)
        if killed:
            print(f"снёс старых демо-проектов: {killed}")

        for title, client, prio, deadline, status, n_build, n_chat, ready, access in DEMO:
            p = Project(
                client=client, title=title, priority=prio, deadline=deadline,
                status=status, chat_ref=f"tg:demo-{client.lower()}",
                price=float(100 * prio), brief={"demo": True}, ready_for_work=ready,
                brief_ready=True,   # демо не гоняет brief.py — считаем ТЗ собранным
            )
            s.add(p)
            await s.flush()
            s.add(ProjectChat(project_id=p.id, transport="telegram",
                              chat_id=f"demo-{p.id}-{client.lower()}", is_primary=True))
            for name, kind, st in access:
                s.add(AccessItem(project_id=p.id, name=name, kind=kind, status=st))
            for i in range(n_build):
                s.add(Task(project_id=p.id, order_idx=i, lane="build",
                           title=f"шаг {i + 1}", prompt="демо-задача, реальной работы нет",
                           status="ready"))
            for i in range(n_chat):
                s.add(Task(project_id=p.id, order_idx=100 + i, lane="chat",
                           title=f"ответ клиенту {i + 1}", prompt="демо-сообщение",
                           status="ready"))
            dl = deadline.isoformat() if deadline else "без дедлайна"
            waiting = [n for n, _, st in access if st != "verified"]
            why = ""
            if not ready:
                why = "  [нет галочки «Готов к работе»]"
            elif waiting:
                why = f"  [ждёт доступы: {', '.join(waiting)}]"
            print(f"  + {title:38s} приоритет={prio} дедлайн={dl:12s} "
                  f"задач={n_build + n_chat}{why}")
        await s.commit()

    print("\nготово. дальше:  DRY_RUN=1 python -m autopilot.main")


if __name__ == "__main__":
    asyncio.run(main())
