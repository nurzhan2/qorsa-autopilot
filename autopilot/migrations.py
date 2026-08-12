"""Миграции схемы. Без alembic — здесь его на порядок больше, чем пользы.

Как это работает:

* `schema_version` хранит номера уже применённых миграций;
* `MIGRATIONS` — упорядоченный список, применяется при старте, по одной,
  каждая в своей транзакции;
* каждая миграция обязана быть **идемпотентной**: она может встретить и пустую
  базу (где `create_all` уже сделал всё нужное), и базу предыдущей версии
  (где половины колонок нет). Отсюда `_add_column` и `IF NOT EXISTS` вместо
  голых ALTER.

Правило: новую миграцию только ДОПИСЫВАТЬ в конец. Менять уже применённую
нельзя — на чужой базе она не переиграется.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from .db import Base, engine

log = logging.getLogger("migr")


# ---------- вспомогательные, все идемпотентные ----------

async def _tables(conn) -> set[str]:
    rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    return {r[0] for r in rows}


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {r[1] for r in rows}


async def _add_column(conn, table: str, column: str, ddl: str) -> bool:
    """ALTER TABLE ... ADD COLUMN, если колонки ещё нет."""
    if table not in await _tables(conn):
        return False
    if column in await _columns(conn, table):
        return False
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    log.info("%s: добавлена колонка %s", table, column)
    return True


# ---------- сами миграции ----------

async def ensure_tables(conn) -> None:
    """Создаёт недостающие ТАБЛИЦЫ и не трогает уже существующие.

    Вызывается в начале каждой миграции: миграция обязана быть самодостаточной.
    База, стоящая на версии 1, не переигрывает первую миграцию — и если бы
    вторая полагалась на её `create_all`, она бы упала на отсутствующей
    таблице. Ровно это и случилось при первом прогоне.

    Колонки этим способом не появляются никогда — только явными ALTER ниже.
    """
    await conn.run_sync(Base.metadata.create_all)


async def m001_baseline(conn) -> None:
    """Фиксация схемы фазы 1."""
    await ensure_tables(conn)


async def m002_ingest(conn) -> None:
    """Фаза 2: переписка, транспорты, привязка чатов."""
    await ensure_tables(conn)
    # база фазы 1 не знает про эти колонки
    await _add_column(conn, "projects", "chat_ref", "VARCHAR")
    await _add_column(conn, "projects", "client_replied_at", "DATETIME")
    await _add_column(conn, "messages", "transport", "VARCHAR(16)")
    await _add_column(conn, "messages", "chat_id", "VARCHAR(64)")
    await _add_column(conn, "chat_messages", "transport", "VARCHAR(16) DEFAULT 'telegram'")

    # старый одиночный tg_chat_id переезжает в project_chats.
    # Саму колонку не удаляем: DROP COLUMN на SQLite — это перестройка таблицы
    # ради экономии нескольких байт, а данные из неё уже перенесены.
    if "tg_chat_id" in await _columns(conn, "projects"):
        await conn.execute(text("""
            INSERT OR IGNORE INTO project_chats (project_id, transport, chat_id, handle, is_primary)
            SELECT id, 'telegram', tg_chat_id, NULL, 1
            FROM projects
            WHERE tg_chat_id IS NOT NULL AND TRIM(tg_chat_id) != ''
        """))
        await conn.execute(text("""
            UPDATE projects SET chat_ref = 'tg:' || tg_chat_id
            WHERE chat_ref IS NULL AND tg_chat_id IS NOT NULL AND TRIM(tg_chat_id) != ''
        """))
        log.info("projects.tg_chat_id перенесён в project_chats")

    # уникальность id сообщения — только внутри своего мессенджера
    await conn.execute(text("DROP INDEX IF EXISTS uq_chat_message_legacy"))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_message "
        "ON chat_messages (transport, chat_id, tg_message_id)"))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_chat "
        "ON project_chats (transport, chat_id)"))


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline: схема фазы 1", m001_baseline),
    (2, "ingest: переписка, транспорты, привязка чатов", m002_ingest),
]


# ---------- применение ----------

async def current_version(conn) -> int:
    if "schema_version" not in await _tables(conn):
        return 0
    row = (await conn.execute(text("SELECT MAX(version) FROM schema_version"))).first()
    return int(row[0]) if row and row[0] is not None else 0


async def migrate() -> int:
    """Применяет всё, что ещё не применено. Возвращает итоговую версию."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        have = await current_version(conn)

    for version, name, fn in MIGRATIONS:
        if version <= have:
            continue
        async with engine.begin() as conn:      # каждая — своей транзакцией
            await fn(conn)
            await conn.execute(
                text("INSERT OR REPLACE INTO schema_version (version, name) VALUES (:v, :n)"),
                {"v": version, "n": name})
        log.info("миграция %s применена: %s", version, name)
        have = version

    return have
