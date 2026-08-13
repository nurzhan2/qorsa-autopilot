"""Задачи, которые делает человек.

На реальном плане 87% работы оказалось `manual` — клики в чужих админках,
а не правка файлов. Значит система обязана быть удобной для человека,
а не только для агента, иначе она бесполезна ровно там, где основной объём.

Схема та же, что и для агента, и это принципиально:

    человек говорит «сделал» → машина проверяет теми же критериями

Отметка «сделал» сама по себе задачу не закрывает. Если критерии не прошли,
задача остаётся открытой с внятным объяснением, что именно не сошлось.
Верить на слово мы не отказываемся из вредности: критерий, который никто
не проверил, — это ровно тот молчаливый пропуск, ради устранения которого
затевалась вся фаза.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from .checks import is_runnable, spec
from .db import Project, Session, Task, utcnow

log = logging.getLogger("manual")

# статус задачи, которую машина принять не может и не притворяется
NEEDS_HUMAN = "needs_human"

# Задача прошла, но все проверки были assisted: вердикт вынесла модель.
# Это не дефект работы — это дефект ДОКАЗАТЕЛЬСТВА, и закрывать по нему
# задачу автоматически нельзя (см. verifier.Verdict)
ASSISTED_ONLY = ("принять автоматически нечем: прошли только проверки с моделью "
                 "(screenshot/llm), детерминированных нет — нужно твоё подтверждение")


def needs_human(defects: list[str]) -> bool:
    """Провал из-за того, что проверять нечем, а не из-за плохого кода.

    Повторять сборку тут бессмысленно: агент не починит отсутствующий
    критерий. Такую задачу надо показать человеку.
    """
    markers = ("нет ни одной исполнимой проверки", "неизвестный тип проверки",
               "проверка не запустится", "human:")
    return any(any(m in d for m in markers) for d in defects or [])


def describe(task: Task) -> str:
    """Задача с критериями — так, чтобы её можно было выполнить и проверить."""
    lines = [f"#{task.id} {task.title}"]
    if task.prompt:
        lines.append(f"    {task.prompt.strip()[:300]}")
    if task.deliverable_ref:
        lines.append(f"    из ТЗ: «{task.deliverable_ref}»")
    if task.depends_on:
        lines.append(f"    после: {', '.join(task.depends_on)}")
    lines.append("    как проверю:")
    for check in task.acceptance or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("type") or "?")
        detail = (check.get("cmd") or check.get("url") or check.get("path")
                  or check.get("criteria") or "")
        selector = f"  [{check['selector']}]" if check.get("selector") else ""
        mark = "  " if is_runnable(check) else "  (машиной не проверяется) "
        lines.append(f"      -{mark}{kind}: {detail}{selector}")
    if not task.acceptance:
        lines.append("      - критериев нет — принять будет нечем")
    if task.risk:
        lines.append(f"    риск: {task.risk[:200]}")
    return "\n".join(lines)


async def pending(project_id: int | None = None) -> list[Task]:
    """Что ждёт живых рук: manual/external и всё, что упёрлось в needs_human."""
    async with Session() as s:
        q = (select(Task)
             .where(Task.lane.in_(("build", "verify")),
                    Task.status.in_(("ready", "pending", NEEDS_HUMAN)),
                    Task.orphaned.is_(False))
             .order_by(Task.project_id, Task.order_idx))
        if project_id:
            q = q.where(Task.project_id == project_id)
        rows = (await s.execute(q)).scalars().all()
    return [t for t in rows
            if t.executor in ("manual", "external") or t.status == NEEDS_HUMAN]


async def report(project_id: int | None = None) -> str:
    """Список для человека — то, что уходит владельцу одним сообщением."""
    rows = await pending(project_id)
    if not rows:
        return "Ручных задач нет."
    async with Session() as s:
        titles = {p.id: p.title for p in
                  (await s.execute(select(Project))).scalars().all()}
    out: list[str] = []
    current = None
    for t in rows:
        if t.project_id != current:
            current = t.project_id
            out.append(f"\n=== {titles.get(current, current)} ===")
        out.append(describe(t) + _flag(t))
    return "\n".join(out).strip()


def waits_for_confirmation(task: Task) -> bool:
    """Задача ждёт не работы, а подтверждения: проверки прошли, но все assisted."""
    return task.status == NEEDS_HUMAN and ASSISTED_ONLY in (task.defects or [])


def _flag(task: Task) -> str:
    if waits_for_confirmation(task):
        # разница существенная: тут делать нечего, надо только решить
        return f"\n    [ЖДЁТ ПОДТВЕРЖДЕНИЯ — проверено только моделью, /confirm {task.id}]"
    if task.status == NEEDS_HUMAN:
        return "\n    [ЖДЁТ РАЗБОРА]"
    return ""


async def submit(task_id: int, verifier) -> tuple[bool, list[str]]:
    """Человек отметил задачу выполненной. Проверяем тем же верификатором.

    Возвращает (принято, дефекты). На слово не верим никому — ни агенту,
    ни себе.
    """
    async with Session() as s:
        task = await s.get(Task, task_id)
        if task is None:
            return False, [f"нет задачи {task_id}"]
        project = await s.get(Project, task.project_id)
        if project is None:
            return False, [f"нет проекта у задачи {task_id}"]
        if task.status == "done":
            return True, []

    runnable = [c for c in (task.acceptance or []) if isinstance(c, dict) and is_runnable(c)]
    if not runnable:
        # Принимать без единой проверки нельзя даже с моих слов: тогда весь
        # смысл критериев теряется. Но и требовать невозможного не будем —
        # задача уходит в needs_human, и решение остаётся за человеком явно
        await _set_status(task_id, NEEDS_HUMAN)
        reasons = []
        for check in task.acceptance or []:
            s_ = spec(check.get("type") if isinstance(check, dict) else None)
            if s_ is None:
                reasons.append(f"неизвестный тип {check!r}")
            elif not (s_.deterministic or s_.assisted):
                reasons.append(f"{s_.kind}: {check.get('criteria')}")
        return False, (["принять нечем: исполнимых критериев нет"] + reasons)

    verdict = await verifier.run(task, project)
    if verdict.confirmed:
        await _set_status(task_id, "done", verdict.defects)
        log.info("задача %s принята: все критерии прошли (%s)", task_id, verdict.summary())
        return True, []

    if verdict.assisted_only:
        # Работа, возможно, сделана хорошо — но доказательства этому нет.
        # Отметка человека плюс мнение модели по-прежнему не критерий
        await _set_status(task_id, NEEDS_HUMAN, [ASSISTED_ONLY])
        log.warning("задача %s ждёт подтверждения владельца: %s",
                    task_id, verdict.summary())
        return False, [ASSISTED_ONLY, f"подтвердить: /confirm {task_id}"]

    await _set_status(task_id, NEEDS_HUMAN, verdict.defects)
    log.warning("задача %s не принята: %s", task_id, "; ".join(verdict.defects)[:300])
    return False, verdict.defects


async def confirm(task_id: int) -> tuple[bool, str]:
    """Владелец берёт вердикт на себя и закрывает задачу.

    Это НЕ приёмка машиной, и притворяться ею не надо: сюда попадают задачи,
    которые машина закрыть отказалась — обычно потому, что все проверки
    оказались assisted. Решение человека законно, но оно должно быть явным
    действием и остаться в истории, а не выглядеть как зелёный тест.

    Поэтому подтвердить можно только то, что действительно ждёт человека:
    `/confirm` на живой задаче — это попытка перепрыгнуть приёмку, а не
    закрыть её.
    """
    async with Session() as s:
        task = await s.get(Task, task_id)
        if task is None:
            return False, f"нет задачи {task_id}"
        if task.status == "done":
            return True, f"задача {task_id} и так закрыта"
        if task.status != NEEDS_HUMAN:
            return False, (f"задача {task_id} в статусе {task.status!r}, а не "
                           f"{NEEDS_HUMAN!r} — подтверждать нечего. "
                           f"Отметить выполненной — /done {task_id}")
        was = list(task.defects or [])
        task.status = "done"
        # что именно перекрыто рукой — иначе через месяц не отличишь
        # подтверждённое от проверенного
        task.last_error = "закрыто владельцем вручную; машина не приняла: " + \
                          ("; ".join(was)[:400] or "причина не записана")
        task.updated_at = utcnow()
        await s.commit()
    log.warning("задача %s закрыта подтверждением владельца, а не проверками: %s",
                task_id, "; ".join(was)[:200])
    return True, f"задача {task_id} закрыта под твою ответственность"


async def _set_status(task_id: int, status: str, defects: list[str] | None = None) -> None:
    async with Session() as s:
        row = await s.get(Task, task_id)
        if row is None:
            return
        row.status = status
        if defects is not None:
            row.defects = defects
        row.updated_at = utcnow()
        await s.commit()
