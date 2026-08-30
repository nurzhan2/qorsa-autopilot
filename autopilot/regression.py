"""Не сломала ли новая задача то, что уже было принято.

Опасение простое и верное: каждая задача проверяется СВОИМИ критериями и
ничем больше. Задача про админку правит `backend/app`, задача про корзину
правит `backend/app` — и вторая может сломать первую, а приёмка второй этого
не заметит: она смотрит только на свои проверки. Через десять задач выясняется,
что половина принятого больше не работает.

Способ поймать это ЕСТЬ, и он не требует ещё одной модели: у уже принятых
задач есть их собственные критерии, и это готовые регрессионные тесты.
Достаточно прогнать их снова.

Два правила, без которых проверка врала бы:

**Сравниваем с базой, а не с ожиданием.** Проверка считается сломанной, только
если она проходила ДО работы этой задачи и перестала ПОСЛЕ. Иначе первый же
неподнятый сервер объявил бы виновной любую задачу: все `http`-критерии
проекта красные, а сделала их такими не она.

**Берём только пересекающиеся по файлам.** Гонять все проверки проекта после
каждой задачи — это минуты на каждую задачу и почти всегда впустую. Смотрим
на те принятые задачи, которые трогают те же каталоги (`paths.overlap`).
Граница честная: если планировщик не назвал путь в критериях, пересечения
мы не увидим и регрессию пропустим.

Модель здесь не участвует вовсе. Это тот же детерминированный код, что и
обычная приёмка, — просто запущенный дважды.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from . import paths
from .checks import is_runnable, spec
from .checks import suspicious as suspicious_reason
from .config import cfg
from .db import Session, Task

log = logging.getLogger("regress")


def key_of(task_id: int, check: dict) -> str:
    parts = [str(check.get(f) or "") for f in ("type", "cmd", "url", "path")]
    return f"{task_id}:" + "|".join(parts)


async def at_risk(task: Task, project_id: int) -> list[tuple[int, dict]]:
    """Проверки принятых задач, которые эта задача может сломать.

    Только детерминированные и только не помеченные `suspicious`: у первых
    вердикт выносит код, а вторые проходят и на невыполненной задаче, то есть
    их падение или прохождение ничего не говорит.
    """
    async with Session() as s:
        done = (await s.execute(
            select(Task).where(Task.project_id == project_id,
                               Task.status == "done",
                               Task.id != task.id))).scalars().all()

    out: list[tuple[int, dict]] = []
    for other in done:
        if not paths.overlap(task, other):
            continue
        for check in (other.acceptance or []):
            if not isinstance(check, dict) or not is_runnable(check):
                continue
            s_ = spec(str(check.get("type") or ""))
            if s_ is None or not s_.deterministic or suspicious_reason(check):
                continue
            out.append((other.id, check))
            if len(out) >= cfg.regression_max_checks:
                return out
    return out


async def snapshot(verifier, task: Task, project) -> dict[str, bool]:
    """Что из этого проходит ПРЯМО СЕЙЧАС. Это и есть база сравнения."""
    if not cfg.regression_check:
        return {}
    pairs = await at_risk(task, project.id)
    if not pairs:
        return {}
    state: dict[str, bool] = {}
    for task_id, check in pairs:
        state[key_of(task_id, check)] = await _passes(verifier, check, task, project)
    green = sum(1 for v in state.values() if v)
    log.info("задача %s: регрессионная база — %s проверок соседних задач, "
             "из них зелёных %s", task.id, len(state), green)
    return state


async def broken(verifier, task: Task, project,
                 before: dict[str, bool]) -> list[str]:
    """Что было зелёным до работы и стало красным после.

    Возвращает описания сломанного. Пусто — ничего не сломано.
    """
    if not before:
        return []
    was_green = [k for k, ok in before.items() if ok]
    if not was_green:
        return []

    pairs = {key_of(tid, c): (tid, c) for tid, c in await at_risk(task, project.id)}
    out: list[str] = []
    for key in was_green:
        pair = pairs.get(key)
        if pair is None:
            continue                      # критерий исчез — не наше дело
        task_id, check = pair
        if await _passes(verifier, check, task, project):
            continue
        out.append(f"сломана уже принятая задача {task_id}: "
                   f"{check.get('type')} {check.get('cmd') or check.get('url') or check.get('path')}")
    if out:
        log.warning("задача %s сломала принятое: %s", task.id, "; ".join(out)[:300])
    return out


async def _passes(verifier, check: dict, task: Task, project) -> bool:
    """Одна проверка. Любая ошибка исполнения = не прошла.

    Трактовать сбой запуска как «наверное, всё хорошо» нельзя: это ровно тот
    молчаливый пропуск, который однажды объявил задачу принятой без единой
    проверки.
    """
    handler = verifier.handlers.get(str(check.get("type") or ""))
    if handler is None:
        return False
    from . import projectdir
    try:
        result = await handler(check, str(projectdir.workspace(project)), task, project)
    except Exception:
        return False
    ok = result[0] if isinstance(result, tuple) else bool(result)
    return bool(ok)
