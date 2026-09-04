"""Доходят ли уведомления до владельца — проверка живьём, а не тестом.

Зачем отдельный скрипт. Проект 8 простоял шесть дней, и никто не заметил;
`readiness.problems()` при этом было пусто, `heartbeat.json` писался. Обе
проверки отвечали на вопрос «жив ли процесс», а не «слышит ли меня
владелец». Разница ровно в этом скрипте.

Поломок оказалось две, и вторая кодом не чинится вовсе:

1. адрес брался из глобального `TG_OWNER_CHAT_ID`, а он пуст — id владельца
   живёт по компаниям в `accounts.toml`. Починено;
2. **Telegram-бот не может написать первым тому, кто не нажал у него Start.**
   Пока чата нет, `sendMessage` отвечает 400 «chat not found», очередь
   считает такой отказ неисправимым и ОТМЕНЯЕТ сообщение. Снаружи —
   тишина. Лечится только рукой владельца.

По умолчанию НИЧЕГО НЕ ОТПРАВЛЯЕТ: `getMe` и `getChat` только читают.
`--send` шлёт одно сообщение — тем же путём `Communicator.notify_owner`,
которым ходят эскалации, чтобы проверялся тракт, а не транспорт.

    .venv/Scripts/python.exe scripts/notify_check.py
    .venv/Scripts/python.exe scripts/notify_check.py --send

Печатается статус и тело ответа, а НЕ `str(исключения)`: у httpx в тексте
исключения лежит URL, а в URL лежит токен бота.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopilot import accounts                                    # noqa: E402
from autopilot.communicator import Communicator                   # noqa: E402
from autopilot.main import build_transports                       # noqa: E402
from autopilot.vault import install_log_masking                   # noqa: E402

PROBE = "Проверка связи Qorsa Autopilot: уведомления владельцу доходят."


def _why(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return type(exc).__name__
    return f"{resp.status_code} {resp.text[:160]}"


async def main(send: bool) -> int:
    install_log_masking()          # иначе токен уедет в консоль из httpx
    companies = accounts.active()
    if not companies:
        print("ни одной активной компании в accounts.toml")
        return 2

    transports = build_transports(companies)
    comm = Communicator(transports=transports, accounts=companies)

    broken = []
    for acc in companies:
        transport = comm.transport_for(acc.code, "telegram")
        chat = comm.owner_chat(acc.code)
        print(f"\n=== {acc.code} ({acc.title}) ===")
        print(f"  адрес владельца : {chat or 'НЕ ЗАДАН'}")
        if transport is None:
            print(f"  бот             : НЕ ПОДНЯТ (нет токена {acc.bot_token_ref})")
            broken.append(acc.code)
            continue
        try:
            me = await transport.call("getMe")
            print(f"  бот             : {me.get('id')} @{me.get('username')}")
        except Exception as e:                                    # noqa: BLE001
            print(f"  бот             : getMe НЕ ПРОШЁЛ — {_why(e)}")
            broken.append(acc.code)
            continue
        if not chat:
            print("  чат с владельцем: адреса нет, писать некуда")
            broken.append(acc.code)
            continue
        try:
            info = await transport.call("getChat", chat_id=chat)
            print(f"  чат с владельцем: ЕСТЬ (type={info.get('type')})")
        except Exception as e:                                    # noqa: BLE001
            print(f"  чат с владельцем: НЕТ — {_why(e)}")
            print(f"  ЧТО СДЕЛАТЬ: открой @{me.get('username')} в Telegram "
                  f"с аккаунта {chat} и нажми Start. Бот не может написать "
                  f"первым тому, кто этого не сделал.")
            broken.append(acc.code)
            continue
        if send:
            await comm.notify_owner(PROBE, account=acc.code)
            print("  отправлено      : смотри Telegram. Если сообщения нет — "
                  "ищи в выводе строку «НЕ МОГУ ПИСАТЬ ВЛАДЕЛЬЦУ»")

    for t in transports:
        close = getattr(t, "close", None)
        if close is not None:
            await close()

    print()
    if broken:
        print(f"НЕ ДОХОДЯТ уведомления по компаниям: {', '.join(broken)}")
        return 1
    print("уведомления доходят по всем компаниям"
          if send else "путь открыт; отправить пробное — с --send")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", action="store_true",
                    help="отправить одно пробное сообщение владельцу")
    raise SystemExit(asyncio.run(main(ap.parse_args().send)))
