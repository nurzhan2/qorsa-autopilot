"""Возврат задачи агенту: «работы не было, проверять нечего».

Живой случай, ради которого функция и понадобилась: задачи 536 и 547 проекта 8
стояли терминальными, а от них по `depends_on` висели пятнадцать готовых задач —
весь остаток заказа. Кнопка на дашборде это умела, CLI нет, и две копии одного
действия начали расходиться.
"""
from __future__ import annotations

import pytest
from conftest import make_project, make_tasks

from autopilot import manual
from autopilot.db import Session, Task, attempts_of, log_attempt


async def _задача(status: str, **kw) -> int:
    project = await make_project()
    (task_id,) = await make_tasks(project.id, 1)
    async with Session() as s:
        t = await s.get(Task, task_id)
        t.status = status
        for key, value in kw.items():
            setattr(t, key, value)
        await s.commit()
    return task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["escalated", "needs_human", "awaiting_keys"])
async def test_requeue_возвращает_из_всех_терминальных(db, status):
    """Все три статуса означают «задача снята с работы» — из любого её
    можно вернуть. `awaiting_keys` тоже: ключ мог появиться."""
    task_id = await _задача(status, attempts=4, defects=["старый дефект"])

    ok, message = await manual.requeue(task_id)

    assert ok, message
    async with Session() as s:
        t = await s.get(Task, task_id)
    assert t.status == "ready"
    assert t.lane == "build"
    assert t.attempts == 0
    assert t.defects == []


@pytest.mark.asyncio
async def test_requeue_чистит_разбор(db):
    """РАЗБОР ОПИСЫВАЕТ СОСТОЯНИЕ, КОТОРОГО БОЛЬШЕ НЕТ.

    На задаче 547 в `diagnosis` лежало «команда падает из-за отсутствия
    tail» — верно на момент разбора и неверно теперь: `tail` нашли в
    git-bash, и приёмка повторяет там критерий сама. `instruction_block()`
    подставляет `work_wrong` в промпт агенту, то есть агент получил бы
    задание чинить поломку, которой нет, вместо настоящей причины.
    """
    from autopilot import crew

    diag = {"verdict": "work_wrong", "why": "нет tail",
            "instruction": "запусти через git-bash"}
    task_id = await _задача("escalated", attempts=4, diagnosis=diag)

    # до возврата разбор долетал бы до промпта
    async with Session() as s:
        assert crew.instruction_block((await s.get(Task, task_id)).diagnosis)

    await manual.requeue(task_id)

    async with Session() as s:
        t = await s.get(Task, task_id)
    assert t.diagnosis is None
    assert crew.instruction_block(t.diagnosis) == ""


@pytest.mark.asyncio
async def test_requeue_снимает_журнал_со_счёта(db):
    """Иначе детектор застревания читает старые строки и снимает задачу
    после ПЕРВОГО же провала — кнопка выглядит рабочей и не работает."""
    task_id = await _задача("escalated", attempts=3)
    for _ in range(3):
        await log_attempt(task_id, project_id=1, lane="build",
                          outcome="failed", counted=True, defects=["одно и то же"])
    assert len(await attempts_of(task_id)) == 3

    await manual.requeue(task_id)

    assert await attempts_of(task_id) == []                  # counted_only
    assert len(await attempts_of(task_id, counted_only=False)) == 3   # но не стёрты


@pytest.mark.asyncio
async def test_requeue_записывает_чья_это_рука(db):
    """Сброс счётчика — решение человека, и по базе это должно быть видно:
    иначе через месяц не отличить его от того, что задача просто не падала."""
    task_id = await _задача("escalated", attempts=4)

    await manual.requeue(task_id)

    async with Session() as s:
        t = await s.get(Task, task_id)
    assert "было 4" in t.last_error
    assert "escalated" in t.last_error


@pytest.mark.asyncio
async def test_requeue_не_трогает_живую_задачу(db):
    """`ready` и `running` возвращать неоткуда — это был бы не возврат,
    а обнуление счётчика идущей работы."""
    for status in ("ready", "running", "done"):
        task_id = await _задача(status, attempts=2)
        ok, message = await manual.requeue(task_id)
        assert ok is False, status
        assert status in message
        async with Session() as s:
            t = await s.get(Task, task_id)
        assert t.attempts == 2, "счётчик живой задачи трогать нельзя"


@pytest.mark.asyncio
async def test_requeue_на_несуществующей(db):
    ok, message = await manual.requeue(10_000_000)
    assert ok is False
    assert "нет задачи" in message
