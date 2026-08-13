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
        # INSERT OR IGNORE глушит не только дубли, но и NOT NULL: колонка,
        # добавленная будущей фазой без server_default, превратила бы перенос
        # данных в тихую потерю. Поэтому сверяем количества явно.
        src = (await conn.execute(text(
            "SELECT COUNT(*) FROM projects "
            "WHERE tg_chat_id IS NOT NULL AND TRIM(tg_chat_id) != ''"))).scalar_one()
        got = (await conn.execute(text(
            "SELECT COUNT(*) FROM project_chats WHERE transport = 'telegram'"))).scalar_one()
        if src and not got:
            raise RuntimeError(
                f"перенос tg_chat_id не сработал: источников {src}, перенесено 0 — "
                "скорее всего в project_chats появилась NOT NULL колонка без server_default")
        log.info("projects.tg_chat_id перенесён в project_chats: %s строк", got)

    # уникальность id сообщения — только внутри своего мессенджера
    await conn.execute(text("DROP INDEX IF EXISTS uq_chat_message_legacy"))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_message "
        "ON chat_messages (transport, chat_id, tg_message_id)"))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_project_chat "
        "ON project_chats (transport, chat_id)"))


async def m003_group_roles(conn) -> None:
    """Фаза 3: групповые чаты, роли участников, готовность брифа."""
    await ensure_tables(conn)
    await _add_column(conn, "chat_messages", "sender_role", "VARCHAR(10) DEFAULT 'client'")
    await _add_column(conn, "projects", "brief_ready", "BOOLEAN DEFAULT 0")
    await _add_column(conn, "access_items", "stale", "BOOLEAN DEFAULT 0")
    await _add_column(conn, "access_items", "source", "VARCHAR(10) DEFAULT 'manual'")
    await _add_column(conn, "project_chats", "is_group", "BOOLEAN DEFAULT 0")

    # direction не выбрасываем — переносим в роли. Раньше "out" означало
    # «написано с моего аккаунта», то есть владельцем
    await conn.execute(text("""
        UPDATE chat_messages
        SET sender_role = CASE WHEN direction = 'out' THEN 'owner' ELSE 'client' END
        WHERE sender_role IS NULL OR sender_role = ''
    """))

    # участники — из того, что уже накопилось в переписке
    await conn.execute(text("""
        INSERT OR IGNORE INTO chat_participants
            (transport, chat_id, sender_id, role, display_name, first_seen)
        SELECT transport, chat_id, sender_id, MIN(sender_role), '', MIN(created_at)
        FROM chat_messages
        WHERE sender_id IS NOT NULL AND TRIM(sender_id) != ''
        GROUP BY transport, chat_id, sender_id
    """))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_participant "
        "ON chat_participants (transport, chat_id, sender_id)"))


async def m004_planner(conn) -> None:
    """Фаза 4: план задач с проверяемой приёмкой."""
    await ensure_tables(conn)
    await _add_column(conn, "tasks", "deliverable_ref", "VARCHAR")
    await _add_column(conn, "tasks", "verify_class", "VARCHAR(10) DEFAULT 'human'")
    await _add_column(conn, "tasks", "executor", "VARCHAR(12) DEFAULT 'manual'")
    await _add_column(conn, "tasks", "depends_on", "JSON")
    await _add_column(conn, "tasks", "estimate_min", "INTEGER DEFAULT 0")
    await _add_column(conn, "tasks", "risk", "VARCHAR")
    await _add_column(conn, "tasks", "orphaned", "BOOLEAN DEFAULT 0")
    await _add_column(conn, "projects", "autonomy_ratio", "FLOAT DEFAULT 0")
    await _add_column(conn, "projects", "planned_at", "DATETIME")

    # Задачи, заведённые до фазы 4 (демо и руками), ничего не знают о классах.
    # Считаем их пригодными для агента: иначе демо перестанет работать,
    # а это единственный способ посмотреть на планировщик вживую
    await conn.execute(text("""
        UPDATE tasks SET verify_class = 'auto', executor = 'claude_code'
        WHERE verify_class IS NULL OR verify_class = '' OR verify_class = 'human'
    """))
    await conn.execute(text("UPDATE tasks SET depends_on = '[]' WHERE depends_on IS NULL"))


async def m005_verifier(conn) -> None:
    """Фаза 5: вторая цифра автономности."""
    await ensure_tables(conn)
    await _add_column(conn, "projects", "autonomy_ratio_time", "FLOAT DEFAULT 0")


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline: схема фазы 1", m001_baseline),
    (2, "ingest: переписка, транспорты, привязка чатов", m002_ingest),
    (3, "группы: роли участников, готовность брифа", m003_group_roles),
    (4, "planner: классы проверяемости, исполнитель, зависимости", m004_planner),
    (5, "verifier: доля автономности по времени", m005_verifier),
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
