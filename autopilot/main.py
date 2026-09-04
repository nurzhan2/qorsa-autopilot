from __future__ import annotations

import asyncio
import logging
import sys

from . import accounts, readiness, roles
from .brief import Brief, BriefRunner
from .communicator import Communicator
from .config import cfg
from .db import init_db, recover_orphan_tasks, sync_accounts
from .executor import Executor
from .ingest import Ingest
from .scheduler import Scheduler
from .transports.max import MaxTransport
from .transports.telegram import TelegramTransport
from .vault import anthropic_key, install_log_masking
from .verifier import Verifier

# консоль Windows по умолчанию не cp65001 — без этого весь русский лог превращается в кракозябры
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("main")

# ставим до того, как что-либо начнёт логироваться: значения секретов не должны
# попасть в консоль и в файлы ни из нашего кода, ни из вывода Claude Code
install_log_masking()


def sheets_configured(sheet_id: str | None = None) -> bool:
    """Плейсхолдер из .env.example — не настройка.

    `SHEET_ID=1AbC...` выглядит заданным, синк стартует и валится каждые
    60 секунд с ERROR. Та же ловушка, что была с `sk-ant-...`.
    """
    from pathlib import Path

    from .vault import _usable
    target = sheet_id if sheet_id is not None else cfg.sheet_id
    return _usable(target) and Path(cfg.google_creds).exists()


def build_stack():
    """Возвращает (executor, verifier). В DRY_RUN — заглушки вместо
    настоящего Claude Code и платного судьи."""
    if cfg.dry_run:
        from .fakes import FakeExecutor, FakeVerifier
        log.warning("DRY_RUN=1 — работают заглушки, Claude Code и судья не вызываются")
        return FakeExecutor(delay=(0.5, 3.0)), FakeVerifier()
    return Executor(), Verifier()


def build_transports(companies) -> list:
    """По одному подключению на КОМПАНИЮ и мессенджер.

    Токен берётся из vault по имени из `bot_token_ref` — в accounts.toml
    лежит только ссылка. Компания без токена просто не поднимается: это не
    повод не работать остальным, но сказать об этом надо вслух.
    """
    from .vault import resolve_secret

    transports = []
    for acc in companies:
        token, where = resolve_secret(acc.bot_token_ref) if acc.bot_token_ref else (None, "")
        if token:
            transports.append(TelegramTransport(token=token, account=acc.code))
            log.info("подключение поднято: %s (%s), токен из: %s",
                     accounts.label(acc.code, "telegram"), acc.title, where)
        else:
            log.warning("нет токена по ссылке %r — %s не поднят",
                        acc.bot_token_ref, accounts.label(acc.code, "telegram"))

        if acc.max_token_ref:
            max_token, _ = resolve_secret(acc.max_token_ref)
            if max_token and cfg.max_mode == "polling":
                transports.append(MaxTransport(token=max_token, account=acc.code))
                log.info("подключение поднято: %s (%s)",
                         accounts.label(acc.code, "max"), acc.title)
            elif max_token:
                log.warning("MAX_MODE=%s — поллер транспорта max не поднимаю, "
                            "ждём webhook (%s)", cfg.max_mode, accounts.label(acc.code))
            else:
                log.warning("нет токена по ссылке %r — %s не поднят",
                            acc.max_token_ref, accounts.label(acc.code, "max"))
    return transports


async def check_owner_reachable(communicator, companies) -> list[str]:
    """Может ли бот компании ВООБЩЕ написать своему владельцу.

    Проверка не про настройку, а про Telegram: **бот не может написать
    первым тому, кто не нажал у него Start**. Пока этого не сделано,
    `sendMessage` возвращает 400 «chat not found», `notify_owner` пишет
    строку в лог, а очередь считает такой отказ неисправимым и ОТМЕНЯЕТ
    сообщение (`_permanent_failure`). Снаружи это выглядит как тишина.

    Проверено живьём 05.09: оба бота подняты и отвечают на `getMe`, и
    ни один из них не имеет чата ни с одним id владельца. То есть шесть
    дней молчания по проекту 8 объясняются не только пустым `cfg.tg_owner`,
    но и этим — и второе кодом не чинится вовсе.

    `getChat` только читает. Держим отдельно от `readiness.problems()`
    намеренно: тот принципиально ничего не запускает и в сеть не ходит,
    а диагностика, которая роняет то, что диагностирует, хуже отсутствующей.
    """
    broken = []
    for acc in companies:
        transport = communicator.transport_for(acc.code, "telegram")
        chat = acc.owner_ids.get("telegram", "")
        if transport is None:
            continue
        if not chat:
            log.error("у компании %s не задан owner_tg_id — уведомления по её "
                      "заказам писать некуда", acc.code)
            broken.append(acc.code)
            continue
        try:
            await transport.call("getChat", chat_id=chat)
        except Exception as e:                                  # noqa: BLE001
            # НЕ str(e): у httpx в тексте исключения лежит URL, а в URL — токен
            resp = getattr(e, "response", None)
            why = f"{getattr(resp, 'status_code', type(e).__name__)}"
            log.error("НЕ МОГУ ПИСАТЬ ВЛАДЕЛЬЦУ компании %s (%s): бот %s не имеет "
                      "чата с %s (%s). Открой этого бота в Telegram с аккаунта "
                      "владельца и нажми Start — до этого ни одно уведомление "
                      "не дойдёт", acc.code, acc.title, acc.bot_token_ref, chat, why)
            broken.append(acc.code)
        else:
            log.info("владелец компании %s достижим: чат %s", acc.code, chat)
    return broken


async def main() -> None:
    await init_db()

    # Чего не хватает установке — СРАЗУ, а не после сессии агента.
    #
    # Раньше это выяснялось провалом задачи в самой дорогой форме: агент
    # отработал шесть минут, приёмка упала, задача ушла человеку — и только
    # тогда стало видно, что на машине нет браузера. А без git приёмка и вовсе
    # не жаловалась: судья получал пустой диф и честно судил пустоту.
    for item in readiness.problems():
        level = log.error if item.blocking else log.warning
        level("%s: %s → %s", item.title, item.what, item.fix)

    # Компании — до всего остального: на них завязаны проекты, транспорты
    # и таблицы. Конфиг источник правды, таблица accounts — его зеркало
    companies = accounts.active()
    ids = await sync_accounts(companies)
    log.info("компаний активно: %s — %s", len(companies),
             ", ".join(f"{a.code} ({a.title}, транспорт {a.transport})"
                       for a in companies))
    off = [a for a in accounts.load() if not a.active]
    if off:
        log.info("компании выключены (active=false): %s",
                 ", ".join(f"{a.code} ({a.title})" for a in off))
    orphans = await recover_orphan_tasks()
    if orphans:
        log.warning("вернул в очередь %s задач, зависших в running после прошлого запуска", orphans)

    transports = build_transports(companies) if cfg.ingest_enabled else []
    # accounts= обязателен: без него Communicator знает бота компании, но не
    # знает АДРЕСА её владельца, и все уведомления уходят в лог
    communicator = Communicator(transports=transports, accounts=companies)
    if transports:
        await check_owner_reachable(communicator, companies)
    executor, verifier = build_stack()
    sched = Scheduler(executor, verifier, communicator)

    tasks = [
        asyncio.create_task(sched.run(), name="scheduler"),
        asyncio.create_task(communicator.pump(), name="pump"),
    ]
    # Синк заводится на ФИЗИЧЕСКУЮ ТАБЛИЦУ, а не на компанию: у нас таблица
    # одна на всех, и три поллера читали бы один лист втроём, споря друг с
    # другом и втрое быстрее выбирая квоту Google
    by_sheet: dict[str, list] = {}
    for acc in companies:
        if not sheets_configured(acc.sheet_id):
            log.warning("синк с таблицей выключен для компании %s: нет sheet_id "
                        "или файла %s", acc.code, cfg.google_creds)
            continue
        by_sheet.setdefault(acc.sheet_id, []).append(acc)

    if by_sheet:
        # Импорт ЗДЕСЬ, а не наверху файла. `sheets.py` тянет gspread, и при
        # импорте на уровне модуля весь автопилот отказывался стартовать без
        # него — даже когда таблица не настроена и синк всё равно не поднялся
        # бы. Работа через окно «Новый заказ» к Google-таблице отношения
        # не имеет, а падало на ней всё.
        from .sheets import SheetSync

    for sheet_id, group in by_sheet.items():
        sync = SheetSync(accounts=group,
                         account_ids={a.code: ids.get(a.code) for a in group},
                         communicator=communicator)
        name = "+".join(a.code for a in group)
        log.info("синк таблицы: компании %s%s", name,
                 " (различаются колонкой «Компания»)" if len(group) > 1 else "")
        tasks.append(asyncio.create_task(sync.loop(), name=f"sheets:{name}"))

    if transports and not roles.owner_configured(companies):
        log.critical("ни у одной компании не задан владелец — ingest выключен. "
                     "Без него собственные реплики уедут в ТЗ как требования клиента")
    elif transports:
        ingest = Ingest(transports, communicator, sched, verifier, accounts=companies)
        tasks.append(asyncio.create_task(ingest.run(), name="ingest"))
    else:
        log.warning("ни один мессенджер не настроен — входящие не принимаются")

    if anthropic_key() and not cfg.dry_run:
        runner = BriefRunner(Brief(communicator=communicator), communicator)
        tasks.append(asyncio.create_task(runner.loop(), name="brief"))
    else:
        log.warning("ключа нет или включён DRY_RUN — бриф не собирается")

    # если одна петля всё-таки умерла — гасим остальные, а не висим полутрупом
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        if t.cancelled():
            continue
        if t.exception() is not None:
            log.error("петля %s упала", t.get_name(), exc_info=t.exception())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
