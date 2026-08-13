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

from sqlalchemy import func, select                                # noqa: E402
from sqlalchemy.exc import IntegrityError                          # noqa: E402

import logging                                                     # noqa: E402

from autopilot import roles                                        # noqa: E402
from autopilot.db import ChatMessage, ProjectChat, Session, init_db  # noqa: E402
from autopilot.secrets_scan import scrub                            # noqa: E402

TRANSPORT = "telegram"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("import")

# Реальный экспорт куда пестрее синтетики. Всё, что не опознали, честно
# помечаем медиа неизвестного вида: пусть попадёт в unreadable[], но не
# притворяется текстом и не роняет импорт.
MEDIA_HINTS = (
    ("photo", "photo"), ("file", "document"), ("sticker_emoji", "sticker"),
    ("location_information", "location"), ("contact_information", "contact"),
    ("poll", "poll"), ("game_title", "game"), ("live_location_period_seconds", "location"),
)

# media_type из экспорта -> наш вид
MEDIA_TYPES = {
    "voice_message": "voice",
    "video_message": "video_note",     # кружок
    "sticker": "sticker",
    "animation": "animation",
    "video_file": "video",
    "audio_file": "audio",
}


def flatten_text(value) -> str:
    """`text` в экспорте бывает строкой, а бывает списком кусков с разметкой.

    Куски — это ссылки, упоминания, код, спойлеры. Нас интересует только
    видимый текст; всё непонятное молча пропускаем, но не роняем импорт.
    """
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
    if value is None:
        return ""
    return str(value)


def media_of(msg: dict) -> tuple[bool, str | None]:
    """Содержимое не качаем — фиксируем факт и вид."""
    raw_type = msg.get("media_type")
    if raw_type:
        return True, MEDIA_TYPES.get(str(raw_type), str(raw_type))
    for key, kind in MEDIA_HINTS:
        if msg.get(key):
            return True, kind
    return False, None


def poll_text(msg: dict) -> str:
    """У опроса нет обычного текста — берём вопрос, ответы не угадываем."""
    poll = msg.get("poll")
    if not isinstance(poll, dict):
        return ""
    question = str(poll.get("question") or "").strip()
    return f"[опрос] {question}" if question else "[опрос]"


def sender_id_of(msg: dict) -> str | None:
    """`from_id` в экспорте выглядит как "user123456789" — нам нужен голый id,
    иначе роли из .env не совпадут ни с чем."""
    raw = str(msg.get("from_id") or "")
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits or raw


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


def make_role_resolver(data: dict, client_id: str | None, owner_id: str | None):
    """Кто есть кто в импортируемом чате.

    В экспорте личной переписки ролей нет вообще — там просто два человека.
    Поэтому:
      1) `--client-id` / `--owner-id` заданы явно — используем их;
      2) иначе для личного чата собеседник (верхнеуровневый `id`) считается
         клиентом, а все остальные отправители — владельцем;
      3) иначе роли берутся из .env, как на живом потоке.
    """
    kind = str(data.get("type") or "")
    counterpart = str(data.get("id") or "").lstrip("-")
    personal = kind.startswith("personal") or kind == "private_chat"

    def resolve(sender: str | None) -> str | None:
        if sender is None:
            return None
        if client_id and sender == str(client_id):
            return roles.CLIENT
        if owner_id and sender == str(owner_id):
            return roles.OWNER
        if client_id or owner_id:
            return None            # задан только один — остальных решает .env
        if personal and counterpart:
            return roles.CLIENT if sender == counterpart else roles.OWNER
        return None

    return resolve


async def resolve_project(chat_id: str, explicit: int | None) -> int | None:
    if explicit:
        return explicit
    async with Session() as s:
        row = (await s.execute(
            select(ProjectChat).where(ProjectChat.transport == TRANSPORT,
                                      ProjectChat.chat_id == chat_id))).scalars().first()
    return row.project_id if row else None


async def account_of_project(project_id: int | None):
    """Компания проекта. Нет проекта — компания по умолчанию из конфига."""
    from autopilot import accounts as accounts_cfg
    from autopilot.db import Account, Project as P
    code = accounts_cfg.DEFAULT_CODE
    if project_id:
        async with Session() as s:
            proj = await s.get(P, int(project_id))
            if proj is not None:
                row = await s.get(Account, proj.account_id)
                if row is not None:
                    code = row.code
    return accounts_cfg.by_code(code) or accounts_cfg.load()[0]


async def run(path: Path, project_id: int | None, chat_id: str | None, dry: bool,
              client_id: str | None = None, owner_id: str | None = None) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    chat = str(chat_id or data.get("id") or "")
    if not chat:
        print("не понял chat_id: в файле нет поля id, задай --chat-id", file=sys.stderr)
        return 2

    await init_db()
    # Компания импортируемого чата: от неё зависит, чей id считается владельцем.
    # Берём у проекта, если он известен, иначе компанию по умолчанию
    account = await account_of_project(project_id)
    project_id = await resolve_project(chat, project_id)
    print(f"чат {chat}, сообщений в файле: {len(messages)}, проект: {project_id or '—'}")

    resolve_role = make_role_resolver(data, client_id, owner_id)

    added = skipped = secrets_found = 0
    service = broken = 0
    service_kinds: dict[str, int] = {}

    for msg in messages:
        if not isinstance(msg, dict):
            broken += 1
            continue
        kind_of_entry = str(msg.get("type") or "")
        if kind_of_entry != "message":
            # вступления в группу, закрепления, звонки, смена названия.
            # Пропускаем, но считаем и показываем: молча терять записи нельзя
            service += 1
            action = str(msg.get("action") or kind_of_entry or "unknown")
            service_kinds[action] = service_kinds.get(action, 0) + 1
            continue

        mid = str(msg.get("id", "")).strip()
        if not mid:
            broken += 1
            log.warning("сообщение без id пропущено: %s", str(msg)[:120])
            continue

        try:
            raw_text = flatten_text(msg.get("text"))
            if not raw_text.strip():
                raw_text = poll_text(msg)
            has_media, kind = media_of(msg)
            sender = sender_id_of(msg)
        except Exception as e:
            # одно кривое сообщение не должно ронять импорт целиком
            broken += 1
            log.warning("сообщение %s не разобралось (%s) — пропущено", mid, e)
            continue

        text, names = scrub(raw_text, project_id=project_id, chat_id=chat)
        secrets_found += len(names)

        # роль: сперва явное указание и эвристика личного чата,
        # иначе — то же правило, что на живом потоке
        forced = resolve_role(sender)
        if forced is not None:
            sender_role = forced
            await roles.remember(account, TRANSPORT, chat, sender, str(msg.get("from") or ""))
        else:
            sender_role = await roles.remember(account, TRANSPORT, chat, sender,
                                               str(msg.get("from") or ""))

        if dry:
            added += 1
            continue

        row = ChatMessage(
            transport=TRANSPORT, chat_id=chat, tg_message_id=mid, project_id=project_id,
            # в личном чате верхнеуровневый id — это собеседник; его сообщения
            # входящие, всё остальное написано с моего аккаунта
            direction="in" if sender_role == roles.CLIENT else "out",
            sender_id=sender,
            sender_role=sender_role,
            text=text, has_media=has_media, media_kind=kind,
            reply_to=str(msg["reply_to_message_id"]) if msg.get("reply_to_message_id") else None,
            raw_json={
                "imported": True,
                "date": msg.get("date"),
                # пересылку сохраняем: это чужие слова, и в брифе они
                # не должны выглядеть как сказанные клиентом
                "forwarded_from": msg.get("forwarded_from"),
                "reactions": len(msg.get("reactions") or []) or None,
                "via_bot": msg.get("via_bot"),
            },
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
    if service:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(service_kinds.items()))
        print(f"сервисных записей пропущено: {service} ({detail})")
    if broken:
        print(f"не разобрано и пропущено: {broken} — смотри WARNING выше")
    if not dry:
        async with Session() as s:
            rows = (await s.execute(
                select(ChatMessage.sender_role, func.count())
                .where(ChatMessage.chat_id == chat)
                .group_by(ChatMessage.sender_role))).all()
        print("роли:", ", ".join(f"{r}={n}" for r, n in rows))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="импорт result.json из Telegram Desktop")
    ap.add_argument("path", type=Path)
    ap.add_argument("--project", type=int, default=None, help="id проекта")
    ap.add_argument("--chat-id", default=None, help="если в файле нет поля id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--client-id", default=None,
                    help="id клиента в экспорте; в личном чате определяется сам")
    ap.add_argument("--owner-id", default=None, help="твой id в экспорте")
    args = ap.parse_args()
    if not args.path.exists():
        print(f"нет файла {args.path}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.path, args.project, args.chat_id, args.dry_run,
                          args.client_id, args.owner_id))


if __name__ == "__main__":
    raise SystemExit(main())
