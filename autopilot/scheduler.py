"""Честный планировщик: полосы + weighted fair queueing по проектам.

Почему не FIFO: проект с 40 задачами полностью блокирует остальных.
WFQ раздаёт время по кругу, вес = приоритет из таблицы + буст по дедлайну.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select, update

from .config import cfg
from .db import AccessItem, Project, Session, Task, decayed_units, spent_today, utcnow
from . import limits
from .llm import LimitReached
from .manual import NEEDS_HUMAN, needs_human, unproven_note

log = logging.getLogger("sched")

LANES = ("chat", "build", "verify")

# Полосы, которые встают при исчерпании суточного бюджета.
# chat работает всегда: клиент не должен молчать из-за денег.
PAID_LANES = ("build", "verify")

# Проект обслуживается в любом статусе, кроме терминального и заблокированного.
# blocked = ждёт человека, done = сдан. Всё остальное — рабочее, включая
# blocked_access: там нет доступов, но переписываться с клиентом надо именно там.
DEAD_STATUSES = ("done", "blocked")

# Полоса build требует двух разрешений от людей: галочки менеджера
# «Готов к работе» и полного чеклиста доступов от клиента.
GATED_LANE = "build"

# Полосы, поднимающие процессы claude. Их суммарный параллелизм ограничен
# отдельно от LANE_*: квота подписки общая с ручной работой владельца.
CLAUDE_LANES = ("build", "verify")

# Сколько единиц WFQ списывается в момент выдачи слота. Списываем ДО работы:
# иначе проект с 40 задачами заберёт все свободные слоты одного тика, ведь
# served_units не меняется, пока задачи не завершились.
DISPATCH_UNITS = 1.0


def weight(p: Project) -> float:
    """Больше вес — чаще обслуживаем."""
    w = {1: 3.0, 2: 2.0, 3: 1.0}.get(p.priority, 2.0)
    if p.deadline:
        days = (p.deadline - dt.date.today()).days
        if days <= 0:
            w *= 4.0        # просрочено — тушим пожар
        elif days <= 3:
            w *= 2.0
    return w


class Scheduler:
    def __init__(self, executor, verifier, communicator):
        self.executor = executor
        self.verifier = verifier
        self.communicator = communicator
        # занятые слоты по полосам — считаем сами, без чтения приватных полей Semaphore
        self.running: dict[str, int] = {ln: 0 for ln in cfg.lane_limits}
        self.busy_projects: set[int] = set()   # проекты с активной эксклюзивной задачей
        self.inflight: set[int] = set()        # id задач в работе
        self._workers: set[asyncio.Task] = set()   # держим ссылки, иначе GC съест задачу на лету
        self.budget_paused = False
        self.tick_sec = 1.0

    def free_slots(self, lane: str) -> int:
        limit = cfg.lane_limits[lane]
        if lane in CLAUDE_LANES:
            # Полоса считает ЗАДАЧИ, а CC_MAX_CONCURRENT — одновременные
            # процессы claude. Это разные вещи: build и verify оба поднимают
            # сессии и вместе съедают окно подписки быстрее, чем каждая
            # по отдельности. Общий потолок — на обе полосы сразу
            busy = sum(self.running[ln] for ln in CLAUDE_LANES)
            limit = min(limit, cfg.cc_max_concurrent - busy + self.running[lane])
        return max(0, limit - self.running[lane])

    @staticmethod
    def build_allowed(now_local: dt.datetime | None = None) -> bool:
        """Можно ли сейчас поднимать headless-сессии Claude Code.

        Они едят ТУ ЖЕ пятичасовую квоту подписки, что и работа владельца
        руками. Тихие часы разводят автопилот и человека по времени: днём
        окно нужно живому человеку, ночью его некому занимать.

        Равные границы выключают ограничение — так и стоит по умолчанию.
        """
        if cfg.quiet_build_start == cfg.quiet_build_end:
            return True
        hour = (now_local or dt.datetime.now()).hour
        if cfg.quiet_build_start < cfg.quiet_build_end:
            quiet = cfg.quiet_build_start <= hour < cfg.quiet_build_end
        else:
            quiet = hour >= cfg.quiet_build_start or hour < cfg.quiet_build_end
        return not quiet

    # ---------- главный цикл ----------

    async def run(self) -> None:
        log.info("scheduler up | lanes=%s", cfg.lane_limits)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(self.tick_sec)

    async def tick(self) -> None:
        """Один проход по всем полосам. Вынесен из run(), чтобы дёргать поштучно из тестов."""
        over_budget = await spent_today() >= cfg.daily_budget_usd
        if over_budget and not self.budget_paused:
            log.warning("СУТОЧНЫЙ БЮДЖЕТ ИСЧЕРПАН ($%.2f) — пауза build/verify, chat работает",
                        cfg.daily_budget_usd)
        elif self.budget_paused and not over_budget:
            log.info("бюджет снова в норме — build/verify разморожены")
        self.budget_paused = over_budget

        await self._sync_access()

        # Квота подписки исчерпана — стоит ВСЯ работа с моделью. Пробовать
        # соседнюю задачу бессмысленно: упрётся ровно так же и сожжёт
        # остаток окна на пустые попытки
        limited = limits.state.blocked()
        if limited:
            log.debug("квота закрыта ещё %.0f мин — платные полосы стоят",
                      limits.state.seconds_left() / 60)

        for lane in LANES:
            if (self.budget_paused or limited) and lane in PAID_LANES:
                continue
            if lane == "build" and not self.build_allowed():
                continue
            await self._dispatch(lane)

    async def drain(self, timeout: float = 30.0) -> None:
        """Дождаться завершения всех запущенных задач (тесты и мягкая остановка)."""
        while self._workers:
            await asyncio.wait(set(self._workers), timeout=timeout)

    # ---------- выбор задачи ----------

    async def _dispatch(self, lane: str) -> None:
        exclusive = cfg.lane_exclusive[lane]
        # за один тик занимаем не больше, чем свободно сейчас: иначе на мгновенных
        # задачах цикл вычерпывает всю очередь полосы и другие полосы ждут
        quota = self.free_slots(lane)

        while quota > 0 and self.free_slots(lane) > 0:
            quota -= 1
            pair = await self._pick(lane, exclusive)
            if pair is None:
                return
            task, project = pair

            if not await self._claim(task.id):
                # снимок из _pick устарел: задачу уже забрали. Берём следующую
                continue

            self.running[lane] += 1
            self.inflight.add(task.id)
            if exclusive:
                self.busy_projects.add(project.id)
            # списываем сразу — следующий _pick в этом же тике увидит новый score
            await self._charge_dispatch(project.id)
            if lane == "build":
                await self._activate(project)

            worker = asyncio.create_task(
                self._work(lane, task, project, exclusive), name=f"{lane}:task{task.id}")
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    async def _pick(self, lane: str, exclusive: bool):
        """Проект с минимальным served_units/weight, из него — самая ранняя задача."""
        blocked = await self._projects_without_access() if lane == GATED_LANE else set()
        async with Session() as s:
            q = (
                select(Task, Project)
                .join(Project, Project.id == Task.project_id)
                .where(Task.lane == lane, Task.status == "ready")
                .where(Project.status.notin_(DEAD_STATUSES))
                .order_by(Task.order_idx, Task.id)
            )
            if lane == GATED_LANE:
                # галочка менеджера: пока не стоит, проект не выходит из briefing
                q = q.where(Project.ready_for_work.is_(True))
                # и собранный бриф: строить по недопонятому ТЗ — способ
                # сделать не то и потратить на это деньги
                q = q.where(Project.brief_ready.is_(True))
                # Класс проверяемости и исполнитель. Задачу, которую нельзя
                # проверить машиной, агенту отдавать бессмысленно: он объявит
                # её сделанной, а проверить это будет нечем
                q = q.where(Task.verify_class != "human")
                q = q.where(Task.executor == "claude_code")
                # требование исчезло из ТЗ — задача ждёт решения человека
                q = q.where(Task.orphaned.is_(False))
            rows = (await s.execute(q)).all()
            done_titles = await self._done_titles(s, lane, rows)

        # ВАЖНО: rows — снимок, снятый до этой строки. Пока мы его фильтруем,
        # какая-то задача успевает доработать и выйти из inflight, оставшись в
        # снимке со статусом ready. Поэтому выбор здесь — только кандидат,
        # право на работу выдаёт _claim() одним условным UPDATE.
        best = None
        best_score = None
        now = utcnow()
        for task, project in rows:
            if task.id in self.inflight:
                continue
            if exclusive and project.id in self.busy_projects:
                continue
            if project.id in blocked:
                continue          # чеклист доступов не закрыт
            if lane == GATED_LANE and not self._deps_done(task, done_titles):
                continue          # зависимости ещё не сделаны
            score = decayed_units(project.served_units, project.served_at, now) / weight(project)
            if best_score is None or score < best_score:
                best, best_score = (task, project), score
        return best

    @staticmethod
    async def _done_titles(s, lane: str, rows) -> dict[int, set[str]]:
        """Названия выполненных задач по проектам — для проверки зависимостей."""
        if lane != GATED_LANE or not rows:
            return {}
        ids = {project.id for _, project in rows}
        done = (await s.execute(
            select(Task.project_id, Task.title)
            .where(Task.project_id.in_(ids), Task.status == "done"))).all()
        out: dict[int, set[str]] = {}
        for pid, title in done:
            out.setdefault(pid, set()).add((title or "").strip().lower())
        return out

    @staticmethod
    def _deps_done(task: Task, done_titles: dict[int, set[str]]) -> bool:
        deps = task.depends_on or []
        if not deps:
            return True
        done = done_titles.get(task.project_id, set())
        return all(str(d).strip().lower() in done for d in deps)

    # ---------- исполнение ----------

    async def _claim(self, task_id: int) -> bool:
        """Атомарно переводит ready -> running. Ровно один захват на задачу:
        условие проверяется и меняется одним UPDATE, а не read-modify-write."""
        async with Session() as s:
            res = await s.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "ready")
                .values(status="running", updated_at=utcnow()))
            await s.commit()
            return res.rowcount == 1

    async def _work(self, lane: str, task: Task, project: Project, exclusive: bool) -> None:
        started = utcnow()
        try:
            if lane == "build":
                await self.executor.run(task, project)
                # собранное уходит на приёмку той же задачей, но в другой полосе
                await self._set_status(task.id, "ready", lane="verify")
            elif lane == "verify":
                verdict = await self.verifier.run(task, project)
                if verdict.confirmed:
                    await self._set_status(task.id, "done")
                    await self.communicator.on_task_done(task, project)
                    await self._maybe_finish(project.id)
                elif verdict.unproven:
                    # Прошло, но доказательства нет: судила модель либо
                    # критерии оказались пустышками. Закрыть такую задачу —
                    # значит приравнять её к зелёному тесту
                    await self._needs_confirmation(task, project, verdict)
                else:
                    await self._on_fail(lane, task, project, verdict.defects)
            elif lane == "chat":
                await self.communicator.process(task, project)
                await self._set_status(task.id, "done")
        except asyncio.CancelledError:
            raise
        except LimitReached as e:
            # Упор в квоту — НЕ провал задачи. Попытку не засчитываем и
            # возвращаем задачу в очередь как есть: четыре упора подряд
            # увели бы живую работу в эскалацию, хотя с ней всё в порядке
            await self._on_limit(task, lane, e)
        except Exception as e:
            log.exception("task %s failed", task.id)
            try:
                await self._on_fail(lane, task, project, [f"exception: {e}"])
            except Exception:
                log.exception("не смог записать провал task %s", task.id)
        finally:
            minutes = (utcnow() - started).total_seconds() / 60
            try:
                await self._charge_done(project.id, minutes)
            except Exception:
                log.exception("не смог списать время project %s", project.id)
            self.inflight.discard(task.id)
            if exclusive:
                self.busy_projects.discard(project.id)
            self.running[lane] -= 1

    async def _on_limit(self, task: Task, lane: str, exc) -> None:
        """Квота кончилась: задача возвращается в очередь нетронутой.

        Ни attempts, ни defects, ни статус не меняются — меняется только
        общее состояние процесса, и встают все полосы сразу. Квота одна
        на всех, и пробовать соседнюю задачу бессмысленно.
        """
        wait = limits.state.hit(str(exc), getattr(exc, "retry_after", None))
        async with Session() as s:
            row = await s.get(Task, task.id)
            if row is not None and row.status == "running":
                row.status = "ready"          # вернуть в очередь как есть
                row.updated_at = utcnow()
                await s.commit()
        log.warning("задача %s отложена из-за квоты на %.0f минут (полоса %s), "
                    "попытка НЕ засчитана", task.id, wait / 60, lane)
        if limits.state.should_notify():
            notify = getattr(self.communicator, "notify_owner", None)
            if notify is not None:
                await notify(limits.state.message())

    async def _needs_confirmation(self, task: Task, project: Project, verdict) -> None:
        """Задача сделана и проверки прошли — но ничего не доказали.

        Это не провал: повторять сборку бессмысленно, агент ничего не чинил бы.
        И не приёмка: доказательством было либо мнение модели, либо критерий,
        который прошёл бы и на невыполненной задаче.
        Поэтому попытка НЕ засчитывается, задача просто ждёт живого решения.
        """
        note = unproven_note(verdict)
        async with Session() as s:
            t = await s.get(Task, task.id)
            if t is None:
                return
            t.status = NEEDS_HUMAN
            t.defects = [note]
            t.updated_at = utcnow()
            await s.commit()
        log.warning("задача %s ждёт подтверждения: %s (%s)",
                    task.id, task.title, verdict.summary())
        notify = getattr(self.communicator, "notify_owner", None)
        if notify is not None:
            # Замечания судьи вне критерия решение не меняют, но человеку,
            # который сейчас будет решать, знать о них полезно
            extra = (f"\nСудья заметил попутно {len(verdict.observations)} вещ(и) "
                     f"вне критерия — они на приёмку не влияли."
                     if getattr(verdict, "observations", ()) else "")
            await notify(f"Задача {task.id} «{task.title}» сделана, но приёмка "
                         f"ничего не доказала — {verdict.why_unproven()}.{extra}\n"
                         f"Посмотреть — /show {task.id}, подтвердить — /confirm {task.id}")

    async def _on_fail(self, lane: str, task: Task, project: Project, defects: list[str]) -> None:
        async with Session() as s:
            t = await s.get(Task, task.id)
            if t is None:
                return
            t.attempts += 1
            t.defects = defects
            if needs_human(defects):
                # Проверять нечем — агент это не починит, сколько ни повторяй.
                # Задача уходит человеку сразу, а не после MAX_ATTEMPTS попыток
                t.status = NEEDS_HUMAN
                t.updated_at = utcnow()
                await s.commit()
                log.warning("задача %s ждёт человека: %s", t.id, "; ".join(defects)[:200])
                return
            if t.attempts >= cfg.max_attempts:
                t.status = "escalated"
                p = await s.get(Project, project.id)
                if p is not None:
                    p.status = "blocked"
                    p.last_action = f"эскалация: {t.title}"
                    p.updated_at = utcnow()
                log.warning("ЭСКАЛАЦИЯ -> %s / %s", project.title, t.title)
            else:
                t.status = "ready"
                if lane == "verify":
                    # не прошло приёмку — обратно на починку, тем же cc_session_id
                    t.lane = "build"
                # упавшие build/chat остаются в своей полосе: перекидывать
                # chat-задачу в build — это менять её смысл
            t.updated_at = utcnow()
            await s.commit()

    async def _set_status(self, task_id: int, status: str, lane: str | None = None) -> None:
        async with Session() as s:
            t = await s.get(Task, task_id)
            if t is None:
                return
            t.status = status
            if lane:
                t.lane = lane
            t.updated_at = utcnow()
            await s.commit()

    # ---------- доступы ----------

    async def _projects_without_access(self) -> set[int]:
        """Проекты, у которых хоть один пункт чеклиста не verified."""
        async with Session() as s:
            rows = (await s.execute(
                select(AccessItem.project_id)
                .where(AccessItem.status != "verified")
                .distinct())).scalars().all()
        return set(rows)

    async def _sync_access(self) -> None:
        """Держит статус blocked_access в согласии с чеклистом и, не чаще
        раза в сутки, напоминает клиенту про недостающее — ОДНИМ сообщением."""
        blocked = await self._projects_without_access()

        async with Session() as s:
            projects = (await s.execute(
                select(Project).where(Project.status.notin_(DEAD_STATUSES)))).scalars().all()
            missing: dict[int, list[AccessItem]] = {}
            if blocked:
                items = (await s.execute(
                    select(AccessItem)
                    .where(AccessItem.project_id.in_(blocked), AccessItem.status != "verified")
                    .order_by(AccessItem.id))).scalars().all()
                for it in items:
                    missing.setdefault(it.project_id, []).append(it)

        to_remind: list[tuple[Project, list[AccessItem]]] = []
        for p in projects:
            waiting = p.id in blocked
            if waiting and p.status in ("new", "briefing", "active"):
                await self._set_project_status(p.id, "blocked_access",
                                               "жду доступы от клиента")
                log.info("проект %s: %s -> blocked_access", p.id, p.status)
            elif not waiting and p.status == "blocked_access":
                await self._set_project_status(p.id, "active", "доступы получены")
                log.info("проект %s: blocked_access -> active", p.id)
            if waiting:
                to_remind.append((p, missing.get(p.id, [])))

        remind = getattr(self.communicator, "remind_access", None)
        if remind is None:
            return
        for project, items in to_remind:
            try:
                await remind(project, items)
            except Exception:
                log.exception("не смог напомнить про доступы, проект %s", project.id)

    async def _set_project_status(self, project_id: int, status: str, note: str = "") -> None:
        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None or p.status == status:
                return
            p.status = status
            if note:
                p.last_action = note
            p.updated_at = utcnow()
            await s.commit()

    # ---------- статусы проекта ----------

    async def _activate(self, project: Project) -> None:
        """new/briefing/blocked_access -> active в момент выдачи первого build-слота.
        Раньше нельзя: пока задач нет, проект действительно ещё брифуется."""
        if project.status not in ("new", "briefing", "blocked_access"):
            return
        async with Session() as s:
            p = await s.get(Project, project.id)
            if p is None or p.status not in ("new", "briefing", "blocked_access"):
                return
            was = p.status
            p.status = "active"
            p.updated_at = utcnow()
            await s.commit()
        project.status = "active"
        log.info("проект %s: %s -> active", project.id, was)

    async def _maybe_finish(self, project_id: int) -> None:
        """active -> review, когда не осталось незакрытых задач.
        review -> done ставит человек: принял работу — закрыл строку в таблице."""
        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None or p.status != "active":
                return
            left = (await s.execute(
                select(Task.id).where(Task.project_id == project_id,
                                      Task.status.notin_(
                                          ("done", "escalated", NEEDS_HUMAN))))).first()
            if left is not None:
                return
            p.status = "review"
            p.last_action = "все задачи закрыты, жду проверки"
            p.updated_at = utcnow()
            await s.commit()
            project = p
        log.info("проект %s: active -> review", project_id)
        # этап закрыт — накопленный отчёт уходит не дожидаясь окна агрегации
        on_stage = getattr(self.communicator, "on_stage_done", None)
        if on_stage is not None:
            await on_stage(project)

    # ---------- WFQ ----------

    async def _charge_dispatch(self, project_id: int) -> None:
        await self._add_units(project_id, DISPATCH_UNITS)

    async def _charge_done(self, project_id: int, minutes: float) -> None:
        # выдача слота уже оплачена, добиваем только превышение над ней
        await self._add_units(project_id, max(minutes - DISPATCH_UNITS, 0.0))

    async def _add_units(self, project_id: int, units: float) -> None:
        if units <= 0:
            return
        async with Session() as s:
            p = await s.get(Project, project_id)
            if p is None:
                return
            now = utcnow()
            # сперва гасим накопленное до текущего момента, потом добавляем новое
            p.served_units = decayed_units(p.served_units, p.served_at, now) + units
            p.served_at = now
            p.updated_at = now
            await s.commit()
