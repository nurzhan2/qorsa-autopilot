"""Кто есть кто в чате. Роль зависит от КОМПАНИИ, а не только от id.

Участников трое: **клиент, владелец и бот**. Менеджер и владелец — один
человек с одного аккаунта, поэтому роль `manager` по умолчанию не занята
никем: `MANAGER_TG_ID` работает алиасом `OWNER_TG_ID`.

**Почему роль считается парой (компания, отправитель).** У владельца два
юрлица и два аккаунта. В чатах Qorsa владелец — это id Qorsa Studio;
в чатах Hustle тот же самый id не владелец, а посторонний. Глобальный
`OWNER_TG_ID` этой разницы не видел: он объявлял бы владельцем оба id
в обеих компаниях сразу. Практический вред конкретный — реплики одного
юрлица, попавшие в чат другого, переставали бы считаться клиентскими
и вылетали бы из ТЗ. Или наоборот: собственные слова уезжали бы в бриф
как требования клиента.

Ошибиться в сторону «клиент» безопаснее: пункт всё равно проверяется
по evidence. Поэтому незнакомый id — клиент, как и раньше.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from .config import cfg
from .db import ChatParticipant, Session

log = logging.getLogger("roles")

CLIENT, MANAGER, OWNER, BOT = "client", "manager", "owner", "bot"
ROLES = (CLIENT, MANAGER, OWNER, BOT)


def known_ids(account, transport: str = "telegram") -> dict[str, str]:
    """{sender_id: role} для конкретной компании и мессенджера.

    Менеджерский id по умолчанию отображается в OWNER: это один и тот же
    человек. Отдельная роль включается MANAGER_SEPARATE=1.
    """
    if account is None:
        return {}
    manager_role = MANAGER if cfg.manager_separate else OWNER
    owner_id = account.owner_ids.get(transport, "")
    bot_id = account.bot_ids.get(transport, "")

    # Менеджер пока общий на все компании: отдельного юрлица у него нет.
    # Когда появится — переедет в accounts.toml тем же полем, что owner
    manager_id = cfg.manager_max_id if transport == "max" else cfg.manager_tg_id

    out: dict[str, str] = {}
    for value, role in ((owner_id, OWNER), (manager_id, manager_role), (bot_id, BOT)):
        key = str(value or "").strip()
        if not key:
            continue
        # владелец выигрывает: если один и тот же id указан и там и там,
        # он не должен вдруг стать менеджером
        if key in out and out[key] == OWNER:
            continue
        out[key] = role
    return out


def role_of(account, sender_id: str | None, transport: str = "telegram") -> str:
    """Роль отправителя В ЧАТАХ ЭТОЙ КОМПАНИИ."""
    if not sender_id:
        return CLIENT
    return known_ids(account, transport).get(str(sender_id), CLIENT)


def owner_configured(accounts) -> bool:
    """Хоть у одной активной компании должен быть указан владелец.

    Без этого система не отличит свои реплики от клиентских, и собственные
    слова уедут в ТЗ как требования. Дешевле не запуститься.
    """
    return any(a.owner_configured() for a in (accounts or []))


def unconfigured(accounts) -> list[str]:
    """Коды компаний без владельца — их называем в сообщении об ошибке."""
    return [a.code for a in (accounts or []) if not a.owner_configured()]


async def remember(account, transport: str, chat_id: str, sender_id: str | None,
                   display_name: str = "", role: str | None = None) -> str:
    """Заносит участника в chat_participants и возвращает его роль.

    `account` — компания, которой принадлежит чат. Один и тот же человек
    в чатах разных компаний может иметь разные роли, и это не ошибка,
    а ровно то поведение, ради которого функция принимает компанию.

    `role` — роль, назначенная снаружи и не подлежащая пересчёту. Нужна при
    импорте старой переписки: там владельца задают ключом `--owner-id`, и
    без этого параметра функция молча пересчитывала роль по конфигу компании
    и писала в участники СВОЙ ответ. В сообщениях роль стояла правильная,
    а в chat_participants — нет, и расхождение всплыло бы позже.
    """
    role = role or role_of(account, sender_id, transport)
    if not sender_id:
        return role
    code = getattr(account, "code", "") or ""
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
            log.info("новый участник %s:%s — %s (%s, компания %s)",
                     transport, chat_id, sender_id, role, code)
            return role
        # роль могли переопределить в конфиге уже после первого сообщения
        if row.role != role:
            log.info("участник %s сменил роль %s -> %s (компания %s)",
                     sender_id, row.role, role, code)
            row.role = role
        if display_name and row.display_name != display_name:
            row.display_name = display_name
        await s.commit()
    return role
