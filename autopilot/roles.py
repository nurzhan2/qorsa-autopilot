"""Кто есть кто в групповом чате.

В группе четверо: клиент, менеджер, владелец и бот. Роль — не косметика:
бриф строится по словам КЛИЕНТА, и реплика менеджера, попавшая в ТЗ,
превращается в требование, которого клиент не выдвигал.

Правило простое и намеренно негибкое: id бота, владельца и менеджера
заданы в .env, все остальные — клиенты. Ошибиться в сторону «клиент»
безопаснее: лишняя реплика в бриф не попадёт, потому что evidence
всё равно проверяется по роли.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from .config import cfg
from .db import ChatParticipant, Session

log = logging.getLogger("roles")

CLIENT, MANAGER, OWNER, BOT = "client", "manager", "owner", "bot"
ROLES = (CLIENT, MANAGER, OWNER, BOT)


def known_ids(transport: str) -> dict[str, str]:
    """{sender_id: role} из .env для конкретного мессенджера."""
    if transport == "max":
        pairs = ((cfg.owner_max_id, OWNER), (cfg.manager_max_id, MANAGER), (cfg.bot_max_id, BOT))
    else:
        pairs = ((cfg.owner_tg_id, OWNER), (cfg.manager_tg_id, MANAGER), (cfg.bot_tg_id, BOT))
    return {str(i).strip(): role for i, role in pairs if str(i).strip()}


def role_of(transport: str, sender_id: str | None) -> str:
    if not sender_id:
        return CLIENT
    return known_ids(transport).get(str(sender_id), CLIENT)


async def remember(transport: str, chat_id: str, sender_id: str | None,
                   display_name: str = "") -> str:
    """Заносит участника в chat_participants и возвращает его роль."""
    role = role_of(transport, sender_id)
    if not sender_id:
        return role
    async with Session() as s:
        row = (await s.execute(
            select(ChatParticipant).where(
                ChatParticipant.transport == transport,
                ChatParticipant.chat_id == str(chat_id),
                ChatParticipant.sender_id == str(sender_id)))).scalars().first()
        if row is None:
            s.add(ChatParticipant(transport=transport, chat_id=str(chat_id),
                                  sender_id=str(sender_id), role=role,
                                  display_name=display_name or ""))
            await s.commit()
            log.info("новый участник %s:%s — %s (%s)", transport, chat_id, sender_id, role)
            return role
        # роль могли переопределить в .env уже после первого сообщения
        if row.role != role:
            log.info("участник %s сменил роль %s -> %s", sender_id, row.role, role)
            row.role = role
        if display_name and row.display_name != display_name:
            row.display_name = display_name
        await s.commit()
    return role
