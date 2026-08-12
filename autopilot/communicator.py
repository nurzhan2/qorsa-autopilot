"""Общение с клиентом. Ничего не уходит мимо гейта."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random

from sqlalchemy import select

from .config import cfg
from .db import Message, Project, Session, Task, utcnow

log = logging.getLogger("comm")

# 🔴 всё, что стоит денег или создаёт обязательство — только с твоего подтверждения
RED_WORDS = ("цена", "стоимость", "скидк", "оплат", "предоплат", "договор", "счёт",
             "срок", "дедлайн", "успе", "гарант", "верн", "компенсац", "отказ")

# чем меньше индекс, тем свободнее гейт
GATE_ORDER = ("green", "yellow", "red")


def classify(text: str) -> str:
    low = text.lower()
    if any(w in low for w in RED_WORDS):
        return "red"
    if "?" in text:
        return "yellow"
    return "green"


def strictest(*gates: str) -> str:
    """Гейт всегда берётся самый строгий из предложенных.

    Иначе вызов draft(..., gate="green") превращается в дыру: подставь в заголовок
    задачи слово «скидка» — и обещание уедет клиенту без подтверждения.
    """
    return max((g for g in gates if g in GATE_ORDER), key=GATE_ORDER.index, default="red")


def human_delay(gate: str) -> dt.timedelta:
    """Мгновенный ответ выглядит как бот. Ждём как живой человек."""
    if gate == "yellow":
        return dt.timedelta(minutes=10)
    return dt.timedelta(seconds=random.randint(40, 900))


def in_quiet_hours(now_local: dt.datetime | None = None) -> bool:
    """Тихие часы считаются по ЛОКАЛЬНОМУ времени машины, а не по UTC:
    клиент спит по своему часовому поясу, а не по гринвичу.
    Равные границы (QUIET_START == QUIET_END) выключают тишину совсем."""
    if cfg.quiet_start == cfg.quiet_end:
        return False
    h = (now_local or dt.datetime.now()).hour
    if cfg.quiet_start < cfg.quiet_end:
        return cfg.quiet_start <= h < cfg.quiet_end
    return h >= cfg.quiet_start or h < cfg.quiet_end


class Communicator:
    def __init__(self, send_fn=None):
        self.send_fn = send_fn or self._stub_send

    async def _stub_send(self, chat_id: str, text: str) -> None:
        log.info("[TG -> %s] %s", chat_id, text)

    async def draft(self, project: Project, text: str, gate: str | None = None) -> Message:
        gate = strictest(classify(text), gate or "green")
        async with Session() as s:
            m = Message(
                project_id=project.id, text=text, gate=gate,
                status="draft" if gate == "red" else "scheduled",
                send_after=None if gate == "red" else utcnow() + human_delay(gate),
            )
            s.add(m)
            await s.commit()
        if gate == "red":
            log.warning("🔴 НУЖНО ТВОЁ ПОДТВЕРЖДЕНИЕ: %s", text[:120])
        return m

    async def on_task_done(self, task: Task, project: Project) -> None:
        text = f"Готово: {task.title}."
        if project.preview_url:
            text += f" Посмотри: {project.preview_url}"
        await self.draft(project, text, gate="green")

    async def process(self, task: Task, project: Project) -> None:
        """Слот полосы chat — сюда вешаешь ингест/брифинг/ответы."""
        return None

    # ---------- фоновая отправка ----------

    async def pump(self) -> None:
        while True:
            try:
                if in_quiet_hours():
                    await asyncio.sleep(60)
                    continue
                await self.pump_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pump failed")
            await asyncio.sleep(15)

    async def pump_once(self) -> int:
        """Отправить всё, чей срок настал. Возвращает число отправленных."""
        now = utcnow()
        sent = 0
        async with Session() as s:
            q = (
                select(Message, Project)
                .join(Project, Project.id == Message.project_id)
                .where(Message.status == "scheduled",
                       Message.gate != "red",          # красное не уходит никогда
                       Message.send_after.isnot(None),
                       Message.send_after <= now)
            )
            for m, p in (await s.execute(q)).all():
                if not p.tg_chat_id:
                    m.status = "cancelled"
                    log.warning("у проекта %s нет TG chat — сообщение %s отменено", p.id, m.id)
                    continue
                try:
                    await self.send_fn(p.tg_chat_id, m.text)
                except Exception:
                    # не помечаем отправленным: попробуем на следующем круге
                    log.exception("не смог отправить сообщение %s", m.id)
                    continue
                m.status, m.sent_at = "sent", now
                sent += 1
            await s.commit()
        return sent
