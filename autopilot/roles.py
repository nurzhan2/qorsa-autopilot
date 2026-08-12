"""Кто есть кто в групповом чате.

Участников трое: **клиент, владелец и бот**. Менеджер и владелец — один
человек с одного аккаунта, поэтому роль `manager` по умолчанию не занята
никем: `MANAGER_TG_ID` работает алиасом `OWNER_TG_ID`.

Механизм ролей сохранён целиком. Если менеджер когда-нибудь отделится и
сядет в группу со своего аккаунта — `MANAGER_SEPARATE=1`, и всё заработает
как раньше, без правок кода.

Роль — не косметика: бриф строится по словам КЛИЕНТА, и чужая реплика,
попавшая в ТЗ, превращается в требование, которого клиент не выдвигал.
Ошибиться в сторону «клиент» безопаснее: пункт всё равно проверяется
по evidence.
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
    """{sender_id: role} из .env для конкретного мессенджера.

    Менеджерский id по умолчанию отображается в OWNER: это один и тот же
    человек. Отдельная роль включается MANAGER_SEPARATE=1.
    """
    manager_role = MANAGER if cfg.manager_separate else OWNER
    if transport == "max":
        pairs = ((cfg.owner_max_id, OWNER), (cfg.manager_max_id, manager_role),
                 (cfg.bot_max_id, BOT))
    else:
        pairs = ((cfg.owner_tg_id, OWNER), (cfg.manager_tg_id, manager_role),
                 (cfg.bot_tg_id, BOT))
    out: dict[str, str] = {}
    for i, role in pairs:
        key = str(i).strip()
        if not key:
            continue
        # владелец выигрывает: если один и тот же id указан и там и там,
        # он не должен вдруг стать менеджером
        if key in out and out[key] == OWNER:
            continue
        out[key] = role
    return out


def owner_configured() -> bool:
    """Без id владельца система не отличит себя от клиента."""
    return bool(str(cfg.owner_tg_id).strip() or str(cfg.owner_max_id).strip())


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
