from __future__ import annotations

import asyncio
import logging
import sys

from .communicator import Communicator
from .config import cfg
from .db import init_db, recover_orphan_tasks
from .executor import Executor
from .ingest import Ingest
from .scheduler import Scheduler
from .sheets import SheetSync
from .transports.max import MaxTransport
from .transports.telegram import TelegramTransport
from .vault import install_log_masking
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


def build_stack():
    """Возвращает (executor, verifier). В DRY_RUN — заглушки вместо
    настоящего Claude Code и платного судьи."""
    if cfg.dry_run:
        from .fakes import FakeExecutor, FakeVerifier
        log.warning("DRY_RUN=1 — работают заглушки, Claude Code и судья не вызываются")
        return FakeExecutor(delay=(0.5, 3.0)), FakeVerifier()
    return Executor(), Verifier()


def build_transports() -> list:
    """Поднимаем только те мессенджеры, для которых есть токен."""
    transports = []
    if cfg.tg_token:
        transports.append(TelegramTransport())
    else:
        log.warning("TG_BOT_TOKEN не задан — Telegram выключен")
    if cfg.max_token:
        if cfg.max_mode == "polling":
            transports.append(MaxTransport())
        else:
            log.warning("MAX_MODE=%s — поллер не поднимаю, ждём webhook", cfg.max_mode)
    return transports


async def main() -> None:
    await init_db()
    orphans = await recover_orphan_tasks()
    if orphans:
        log.warning("вернул в очередь %s задач, зависших в running после прошлого запуска", orphans)

    transports = build_transports() if cfg.ingest_enabled else []
    communicator = Communicator(transports=transports)
    executor, verifier = build_stack()
    sched = Scheduler(executor, verifier, communicator)

    tasks = [
        asyncio.create_task(sched.run(), name="scheduler"),
        asyncio.create_task(communicator.pump(), name="pump"),
    ]
    if cfg.sheet_id:
        tasks.append(asyncio.create_task(SheetSync().loop(), name="sheets"))
    else:
        log.warning("SHEET_ID не задан — синк с таблицей выключен")
    if transports:
        ingest = Ingest(transports, communicator, sched)
        tasks.append(asyncio.create_task(ingest.run(), name="ingest"))
    else:
        log.warning("ни один мессенджер не настроен — входящие не принимаются")

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
