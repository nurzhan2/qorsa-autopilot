"""Импорт истории переписки из экспорта Telegram Desktop.

Bot API историю не отдаёт — до подключения бота её взять больше неоткуда.

Как сделать экспорт:
  1. Telegram Desktop (не мобильный и не веб) → нужный чат
  2. ⋮ → Export chat history
  3. Снять галочки со всех медиа (они не нужны — мы фиксируем только факт)
  4. Format: **Machine-readable JSON**, Path: любая папка
  5. Экспорт кладёт `result.json`

Запуск:
  python scripts/import_tg_export.py path/to/result.json --project 7
  python scripts/import_tg_export.py path/to/result.json --chat-id 123456789 --dry-run

Секреты перехватываются ровно так же, как на живом потоке: пароль из старой
переписки не окажется в базе открытым. Дубли с уже полученными live-сообщениями
исключены уникальным индексом (transport, chat_id, tg_message_id).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:                     # под pytest stdout подменён и reconfigure может не быть
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import select                                      # noqa: E402
from sqlalchemy.exc import IntegrityError                          # noqa: E402

from autopilot.db import ChatMessage, ProjectChat, Session, init_db  # noqa: E402
from autopilot.secrets_scan import scrub                            # noqa: E402

TRANSPORT = "telegram"

MEDIA_HINTS = (
    ("photo", "photo"), ("file", "document"), ("media_type", None),
    ("sticker_emoji", "sticker"), ("location_information", "location"),
    ("contact_information", "contact"),
)


def flatten_text(value) -> str:
    """`text` в экспорте бывает строкой, а бывает списком кусков с разметкой."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for piece in value:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict):
                parts.append(str(piece.get("text", "")))
        return "".join(parts)
    return ""


def media_of(msg: dict) -> tuple[bool, str | None]:
    if msg.get("media_type"):
        return True, str(msg["media_type"])
    for key, kind in MEDIA_HINTS:
        if key != "media_type" and msg.get(key):
            return True, kind
    return False, None


def parse_date(msg: dict) -> dt.datetime:
    unix = msg.get("date_unixtime")
    if unix:
        return dt.datetime.fromtimestamp(int(unix), dt.timezone.utc)
    raw = msg.get("date")
    if raw:
        try:
            return dt.datetime.fromisoformat(raw).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return dt.datetime.now(dt.timezone.utc)


async def resolve_project(chat_id: str, explicit: int | None) -> int | None:
    if explicit:
        return explicit
    async with Session() as s:
        row = (await s.execute(
            select(ProjectChat).where(ProjectChat.transport == TRANSPORT,
                                      ProjectChat.chat_id == chat_id))).scalars().first()
    return row.project_id if row else None


async def run(path: Path, project_id: int | None, chat_id: str | None, dry: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    chat = str(chat_id or data.get("id") or "")
    if not chat:
        print("не понял chat_id: в файле нет поля id, задай --chat-id", file=sys.stderr)
        return 2

    await init_db()
    project_id = await resolve_project(chat, project_id)
    print(f"чат {chat}, сообщений в файле: {len(messages)}, проект: {project_id or '—'}")

    added = skipped = secrets_found = 0
    for msg in messages:
        if msg.get("type") != "message":
            continue                                  # сервисные события пропускаем
        mid = str(msg.get("id", ""))
        if not mid:
            continue
        raw_text = flatten_text(msg.get("text"))
        text, names = scrub(raw_text, project_id=project_id, chat_id=chat)
        secrets_found += len(names)
        has_media, kind = media_of(msg)
        sender = msg.get("from_id")

        if dry:
            added += 1
            continue

        row = ChatMessage(
            transport=TRANSPORT, chat_id=chat, tg_message_id=mid, project_id=project_id,
            # в личном чате верхнеуровневый id — это собеседник; его сообщения
            # входящие, всё остальное написано с моего аккаунта
            direction="in" if str(sender or "") == f"user{chat}" else "out",
            sender_id=str(sender) if sender else None,
            text=text, has_media=has_media, media_kind=kind,
            reply_to=str(msg["reply_to_message_id"]) if msg.get("reply_to_message_id") else None,
            raw_json={"imported": True, "date": msg.get("date")},
            created_at=parse_date(msg),
        )
        async with Session() as s:
            s.add(row)
            try:
                await s.commit()
                added += 1
            except IntegrityError:
                await s.rollback()
                skipped += 1        # уже прилетело живым потоком — так и должно быть

    verb = "нашлось бы" if dry else "добавлено"
    print(f"{verb}: {added}, пропущено дублей: {skipped}, перехвачено секретов: {secrets_found}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="импорт result.json из Telegram Desktop")
    ap.add_argument("path", type=Path)
    ap.add_argument("--project", type=int, default=None, help="id проекта")
    ap.add_argument("--chat-id", default=None, help="если в файле нет поля id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.path.exists():
        print(f"нет файла {args.path}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.path, args.project, args.chat_id, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
