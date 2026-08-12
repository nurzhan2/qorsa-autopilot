"""Ручная привязка чата к проекту.

Нужна, когда менеджер не заполнил «Чат клиента» и сообщение упало
в неопознанные. Владельцу приходит уведомление со списком проектов —
привязать можно ответом «/bind <id>» прямо в Telegram или отсюда:

    python scripts/bind_chat.py --list
    python scripts/bind_chat.py --project 7 --transport telegram --chat 123456789

Уже сохранённые сообщения этого чата получают project_id задним числом —
ничего не теряется.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:                     # под pytest stdout подменён и reconfigure может не быть
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import func, select                                   # noqa: E402

from autopilot.db import ChatMessage, Project, ProjectChat, Session, init_db  # noqa: E402
from autopilot.ingest import Ingest                                   # noqa: E402


async def show() -> None:
    async with Session() as s:
        print("Проекты:")
        for p in (await s.execute(select(Project).order_by(Project.id))).scalars():
            chats = (await s.execute(
                select(ProjectChat).where(ProjectChat.project_id == p.id))).scalars().all()
            where = ", ".join(f"{c.transport}:{c.chat_id}" for c in chats) or "нет чата"
            print(f"  {p.id:3d} {p.status:15s} {p.title:38s} [{where}]")

        rows = (await s.execute(
            select(ChatMessage.transport, ChatMessage.chat_id, func.count(),
                   func.max(ChatMessage.created_at))
            .where(ChatMessage.project_id.is_(None))
            .group_by(ChatMessage.transport, ChatMessage.chat_id))).all()
    if rows:
        print("\nНеопознанные чаты:")
        for transport, chat_id, n, last in rows:
            print(f"  {transport}:{chat_id}  сообщений={n}  последнее={last}")
    else:
        print("\nНеопознанных чатов нет.")


async def bind(project_id: int, transport: str, chat_id: str) -> int:
    ok = await Ingest([], communicator=None).bind_chat(project_id, transport, chat_id)
    print("привязал" if ok else f"нет проекта {project_id}")
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="привязка чата к проекту")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--project", type=int)
    ap.add_argument("--transport", default="telegram", choices=["telegram", "max"])
    ap.add_argument("--chat")
    args = ap.parse_args()

    async def run() -> int:
        await init_db()
        if args.list or not (args.project and args.chat):
            await show()
            return 0
        return await bind(args.project, args.transport, args.chat)

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
