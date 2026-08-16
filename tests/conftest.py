"""Общая обвязка тестов.

DB_URL подменяется ДО импорта autopilot: engine создаётся на импорте модуля db,
позже его уже не перенаправить. Настоящий Claude Code и Google API не дёргаются
нигде — вместо них autopilot.fakes и локальные заглушки.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qorsa-tests-"))
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["WORKSPACES"] = str(_TMP / "workspaces")
os.environ["LOGS"] = str(_TMP / "logs")
os.environ["ANTHROPIC_API_KEY"] = ""       # судья в тестах не поднимается
# В тестах модель подменяется заглушкой через client=, а это путь API.
# Боевое умолчание — cli, и без этой строки тесты пошли бы искать
# настоящий `claude` в PATH. Тесты самого CLI-бэкенда ставят его явно.
os.environ["LLM_BACKEND"] = "api"
os.environ["SHEET_ID"] = ""
# Тихие часы по умолчанию 23–9. Без этого половина тестов про отправку
# клиенту зелёная днём и красная ночью — ловушка, на которую я уже попался.
os.environ["QUIET_START"] = "0"
os.environ["QUIET_END"] = "0"
# менеджер и владелец — один аккаунт; отдельная роль включается точечно
os.environ["MANAGER_SEPARATE"] = "0"
os.environ["OWNER_TG_ID"] = "777"
# Компании берём из .env, а не из accounts.toml владельца: иначе тесты
# начинают зависеть от того, какие юрлица он себе завёл
os.environ["ACCOUNTS_FILE"] = str(_TMP / "no-such-accounts.toml")

from autopilot.db import (AccessItem, Account, Base, ChatMessage, Project,  # noqa: E402
                          ProjectChat, Session, Task, engine)


@pytest.fixture
async def db():
    """Чистая база на каждый тест.

    dispose() обязателен: pytest-asyncio даёт каждому тесту свой event loop,
    а соединения из пула привязаны к тому loop, в котором открылись.
    """
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


AUTO_CHAT = "<auto>"


async def make_account(code: str = "qorsa", name: str | None = None,
                       owner_tg_id: str = "777", **kw) -> Account:
    """Компания в БД. Идемпотентна: повторный вызов возвращает существующую.

    Отдельная функция нужна почти каждому тесту: проект без компании
    не создаётся вовсе, и это намеренно (см. db.Project.account_id).
    """
    async with Session() as s:
        row = (await s.execute(select(Account).where(Account.code == code))).scalars().first()
        if row is None:
            row = Account(code=code, name=name or code.title(),
                          owner_tg_id=owner_tg_id, signature=name or code.title(), **kw)
            s.add(row)
            await s.commit()
        return row


async def make_project(status: str = "active", priority: int = 2,
                       deadline: dt.date | None = None, title: str = "проект",
                       tg_chat_id: str | None = AUTO_CHAT,
                       ready_for_work: bool = True,
                       brief_ready: bool = True,
                       transport: str = "telegram",
                       account: str = "qorsa") -> Project:
    """tg_chat_id остался для читаемости тестов — под ним создаётся ProjectChat."""
    acc = await make_account(account)
    async with Session() as s:
        p = Project(account_id=acc.id,
                    client="клиент", title=title, status=status, priority=priority,
                    deadline=deadline, ready_for_work=ready_for_work,
                    brief_ready=brief_ready,
                    chat_ref=(f"{'max' if transport == 'max' else 'tg'}:{tg_chat_id}"
                              if tg_chat_id else None))
        s.add(p)
        await s.flush()
        if tg_chat_id == AUTO_CHAT:
            # chat_id уникален в пределах транспорта — на проект свой
            tg_chat_id = f"chat-{p.id}"
            p.chat_ref = f"tg:{tg_chat_id}"
        if tg_chat_id:
            s.add(ProjectChat(project_id=p.id, transport=transport,
                              chat_id=tg_chat_id, is_primary=True))
        await s.commit()
    return p


async def add_chat(project_id: int, transport: str, chat_id: str,
                   handle: str | None = None, is_primary: bool = False) -> ProjectChat:
    async with Session() as s:
        c = ProjectChat(project_id=project_id, transport=transport, chat_id=chat_id,
                        handle=handle, is_primary=is_primary)
        s.add(c)
        await s.commit()
    return c


async def make_access(project_id: int, name: str = "FTP", kind: str = "ftp",
                      status: str = "needed") -> AccessItem:
    async with Session() as s:
        item = AccessItem(project_id=project_id, name=name, kind=kind, status=status)
        s.add(item)
        await s.commit()
    return item


async def make_tasks(project_id: int, n: int, lane: str = "build",
                     status: str = "ready", start_idx: int = 0,
                     verify_class: str = "auto", executor: str = "claude_code",
                     depends_on: list | None = None) -> list[int]:
    """По умолчанию задачи пригодны для агента и машинно проверяемы —
    тесты планировщика полос про распределение времени, а не про классы."""
    ids = []
    async with Session() as s:
        for i in range(n):
            t = Task(project_id=project_id, order_idx=start_idx + i, lane=lane,
                     title=f"{lane} {i + 1}", status=status,
                     verify_class=verify_class, executor=executor,
                     depends_on=list(depends_on or []))
            s.add(t)
            await s.flush()
            ids.append(t.id)
        await s.commit()
    return ids


class FakeCommunicator:
    """Считает вызовы, ничего никуда не шлёт."""

    def __init__(self):
        self.processed: list[int] = []
        self.done: list[int] = []
        self.stages: list[int] = []
        self.reminders: list[tuple[int, int]] = []   # (project_id, сколько пунктов)
        self.owner_notes: list[str] = []

    async def process(self, task, project) -> None:
        self.processed.append(project.id)

    async def on_task_done(self, task, project) -> None:
        self.done.append(project.id)

    async def on_stage_done(self, project) -> None:
        self.stages.append(project.id)

    async def ask_questions(self, project, questions) -> None:
        self.reminders.append((project.id, len(questions)))

    async def remind_access(self, project, items) -> None:
        self.reminders.append((project.id, len(items)))

    async def notify_owner(self, text) -> None:
        self.owner_notes.append(text)
