"""Бриф → задачи с проверяемой приёмкой.

Это ядро проекта, и главное требование к нему — **честность о проверяемости**.
Чат даёт требования, но не даёт критериев приёмки. «Каталог с фильтрами по
типу кожи» — как код поймёт, что это сделано? Никак, если не написать проверку.

Поэтому каждая задача относится к одному из трёх классов:

* `auto`     — есть машинная проверка: сборка проходит, HTTP отдаёт 200,
               селектор присутствует, файл существует. Код проверяет сам.
* `assisted` — проверка возможна, но с оговорками: скриншот оценивает модель,
               результат — подсказка, а не приговор.
* `human`    — проверить машинно нельзя в принципе. «Нравится дизайн»,
               «выглядит премиально», совпадение с макетом.

**Класс определяет код, а не модель.** Модель склонна называть `auto` всё
подряд; после генерации мы смотрим на сами критерии и понижаем класс, если
детерминированной проверки среди них нет. Задача класса `human` не попадает
в полосу build никогда.

Второе — **пригодность для агента**. Установка плагина в админке WordPress,
настройка счётчика Метрики, заливка товаров через веб-интерфейс — это не
работа для Claude Code. Поле `executor` отделяет то, что агент действительно
может сделать в репозитории, от того, что придётся делать руками.

Третье — **происхождение**. Как у брифа evidence, у задачи есть
`deliverable_ref`: ссылка на пункт брифа. Задача без неё выбрасывается —
план не должен содержать работы, которую никто не просил.
"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import select

from .brief import (BriefFailed, SecretLeak, assert_no_secrets, item_text,
                    same_item)
from .config import cfg
from .db import AccessItem, Project, Run, Session, Task, utcnow
from .vault import anthropic_key, missing_secret_message
from .vault import vault as default_vault

log = logging.getLogger("plan")

VERIFY_CLASSES = ("auto", "assisted", "human")
EXECUTORS = ("claude_code", "manual", "external")

# Проверки, которые выполняет код без участия человека и модели.
# Ровно они и дают право называться auto — см. verifier.py.
DETERMINISTIC_CHECKS = ("shell", "http", "file_exists", "dom")
# Проверки, где вердикт выносит модель. Подсказка, не приговор.
ASSISTED_CHECKS = ("llm", "screenshot")
KNOWN_CHECKS = DETERMINISTIC_CHECKS + ASSISTED_CHECKS + ("human",)

# Обязательные поля у каждого вида проверки: без них проверка не запустится,
# а значит она не проверка, а обещание
CHECK_FIELDS = {
    "shell": ("cmd",),
    "http": ("url",),
    "file_exists": ("path",),
    "dom": ("url", "selector"),
    "llm": ("criteria",),
    "screenshot": ("url",),
    "human": ("criteria",),
}

# Признаки того, что работа лежит вне репозитория и агенту недоступна
MANUAL_HINTS = (
    "админк", "панел", "вручную", "руками", "личный кабинет", "интерфейс",
    "зарегистрир", "оплат", "счётчик метрики", "счетчик метрики",
    "загрузить через", "залить через", "настроить в кабинете",
)

SYSTEM = """Ты технический директор. Из готового ТЗ ты собираешь план работ.

Твоя главная задача — НЕ придумать красивые задачи, а честно сказать, что
из этого можно проверить машиной, а что нельзя.

ЖЕЛЕЗНЫЕ ПРАВИЛА:

1. Каждая задача происходит из пункта ТЗ. В deliverable_ref пиши ТОЧНЫЙ текст
   пункта, из которого задача взялась. Задача без deliverable_ref будет
   выброшена: план не должен содержать работы, которую никто не просил.

2. acceptance — это КОД, который выполнится, а не пожелание. Типы:
     shell        {"type":"shell","cmd":"npm run build"}            — команда, код возврата 0
     http         {"type":"http","url":"https://.../","expect":200} — запрос и ожидаемый статус
     file_exists  {"type":"file_exists","path":"wp-content/..."}    — файл на месте
     dom          {"type":"dom","url":"...","selector":".filter"}   — селектор есть на странице
     llm          {"type":"llm","criteria":"..."}                   — судит модель по дифу
     human        {"type":"human","criteria":"..."}                 — судит человек глазами
   Не выдумывай проверку, которой не будет. «Сайт работает» — это не проверка.
   «curl -f https://site/ отдаёт 200» — проверка.

3. verify_class — честная оценка:
     auto     — среди acceptance есть shell/http/file_exists/dom
     assisted — проверить можно только глазами модели (скриншот, диф)
     human    — проверить машиной нельзя в принципе: «нравится дизайн»,
                «совпадает с макетом», «выглядит дорого»
   Не завышай. Код всё равно перепроверит и понизит класс.

4. executor — кто физически делает работу:
     claude_code — правка файлов в репозитории, код, конфиги, сборка
     manual      — действия в чужом веб-интерфейсе: админка CMS, панель
                   хостинга, кабинет аналитики, заливка контента руками
     external    — ждём третью сторону: клиент, платёжный провайдер, дизайнер
   Агент не умеет кликать в админке. Если задача — «установить плагин
   в WordPress через админку», это manual, а не claude_code.

5. depends_on — список title задач, без которых эту начинать бессмысленно.
   Цикл недопустим. Не выдумывай зависимости ради красоты графа.

6. Доступы (хостинг, домен, аналитика) уже отслеживаются отдельным чеклистом.
   НЕ создавай задачи вида «получить доступ к панели» — это не работа.

Отвечай СТРОГО одним JSON-объектом без markdown:
{
  "tasks": [
    {
      "title": "коротко и по делу",
      "description": "что именно сделать",
      "deliverable_ref": "точный текст пункта ТЗ",
      "acceptance": [{"type": "...", ...}],
      "verify_class": "auto|assisted|human",
      "executor": "claude_code|manual|external",
      "depends_on": ["title другой задачи"],
      "estimate_min": 60,
      "risk": "что может пойти не так"
    }
  ]
}"""


class PlanFailed(RuntimeError):
    """Модель не смогла отдать валидный план."""


class CycleInPlan(RuntimeError):
    """Граф зависимостей зациклен. Разруливать это гаданием нельзя."""


# ---------- проверки, которые делает КОД ----------

def valid_checks(acceptance) -> list[dict]:
    """Оставляет только те проверки, которые действительно можно выполнить."""
    out = []
    for check in acceptance or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("type") or "")
        if kind not in KNOWN_CHECKS:
            continue
        if not all(str(check.get(f) or "").strip() for f in CHECK_FIELDS[kind]):
            continue        # проверка без обязательного поля не запустится
        out.append(check)
    return out


def classify(task: dict, declared: str | None = None) -> str:
    """Класс проверяемости. Код умеет ТОЛЬКО ПОНИЖАТЬ, никогда не повышать.

    Два разных промаха, и оба надо ловить:

    * модель заявила `auto`, а детерминированных проверок нет — обещание
      автоматической приёмки враньё, понижаем;
    * модель честно сказала `human` («вёрстка должна совпадать с макетом»),
      но прицепила к задаче `http 200`. Первая версия кода на этом основании
      поднимала класс до `auto` — и весь план оказался «100% авто», хотя
      проверка «сайт отдаёт 200» ничего не говорит о совпадении с макетом.
      Признание модели в непроверяемости весомее наличия формальной проверки.

    Отдельно: если среди критериев есть `human`, машина последнего слова не
    имеет по определению — класс не выше `human`.
    """
    kinds = {c["type"] for c in valid_checks(task.get("acceptance"))}
    if "human" in kinds:
        capable = "human"
    elif kinds & set(DETERMINISTIC_CHECKS):
        capable = "auto"
    elif kinds & set(ASSISTED_CHECKS):
        capable = "assisted"
    else:
        capable = "human"

    if declared not in VERIFY_CLASSES:
        return capable
    # берём строгий из двух: индекс растёт от auto к human
    return max(capable, declared, key=VERIFY_CLASSES.index)


def pick_executor(task: dict, declared: str) -> str:
    """Агент работает с файлами в репозитории. Всё остальное — не его.

    Модель склонна считать, что Claude Code может всё, включая клики
    в админке WordPress. Проверяем по признакам: есть ли работа с файлами
    и командами вообще.
    """
    if declared in ("manual", "external"):
        return declared            # понизить модель вправе, повысить — нет

    text = " ".join(str(task.get(f) or "") for f in ("title", "description")).lower()
    if any(hint in text for hint in MANUAL_HINTS):
        return "manual"

    kinds = {c["type"] for c in valid_checks(task.get("acceptance"))}
    # признак работы в репозитории: команда сборки, проверка файла или дифа
    if kinds & {"shell", "file_exists"}:
        return "claude_code"
    if kinds & {"http", "dom"} and "llm" not in kinds:
        # HTTP-проверка без файлов — скорее всего настройка снаружи,
        # но если задача про код, модель обычно даёт ещё и shell
        return "claude_code" if declared == "claude_code" else "manual"
    return "manual"


def topological_order(tasks: list[dict]) -> list[str]:
    """Порядок выполнения. Цикл — исключение, а не попытка его разрулить."""
    titles = [t["title"] for t in tasks]
    deps = {t["title"]: [d for d in (t.get("depends_on") or []) if d in titles]
            for t in tasks}
    order: list[str] = []
    state: dict[str, int] = {}          # 0 не тронут, 1 в обходе, 2 готов

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = " -> ".join(path + [node])
            raise CycleInPlan(f"цикл в зависимостях: {cycle}")
        state[node] = 1
        for dep in deps.get(node, []):
            visit(dep, path + [node])
        state[node] = 2
        order.append(node)

    for title in titles:
        visit(title, [])
    return order


def autonomy(tasks: list[dict]) -> dict:
    """Доля работы, которую система закроет без человека."""
    total = len(tasks) or 1
    by_class = {c: sum(1 for t in tasks if t["verify_class"] == c) for c in VERIFY_CLASSES}
    by_executor = {e: sum(1 for t in tasks if t["executor"] == e) for e in EXECUTORS}
    # автономно закрывается только то, что агент и сделает, и проверит
    autonomous = sum(1 for t in tasks
                     if t["verify_class"] == "auto" and t["executor"] == "claude_code")
    return {
        "tasks": len(tasks),
        "by_class": by_class,
        "by_executor": by_executor,
        "auto_ratio": round(autonomous / total, 2),
        "suitable": autonomous / total >= cfg.autonomy_min_ratio,
    }


class Planner:
    def __init__(self, client=None, vault=None, communicator=None, model: str | None = None):
        self._client = client
        self.vault = vault or default_vault
        self.communicator = communicator
        self.model = model or cfg.plan_model

    @property
    def client(self):
        if self._client is None:
            key = anthropic_key(self.vault)
            if key:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=key)
        return self._client

    # ---------- промпт ----------

    def build_prompt(self, project: Project, brief: dict,
                     access: list[AccessItem]) -> str:
        lines = [f"# Проект\n{project.title} (клиент: {project.client})"]
        goal = brief.get("goal")
        if goal:
            lines.append(f"# Цель\n{goal.get('text')}")

        parts = []
        for field in ("deliverables", "stack", "constraints", "assets", "out_of_scope"):
            items = brief.get(field) or []
            if not items:
                continue
            rows = []
            for item in items:
                mark = " [ОТСУТСТВУЕТ В ПОСЛЕДНЕМ ПРОГОНЕ]" if item.get("missing") else ""
                prio = f" ({item['priority']})" if item.get("priority") else ""
                rows.append(f"- {item_text(item)}{prio}{mark}")
            parts.append(f"## {field}\n" + "\n".join(rows))
        lines.append("# ТЗ\n" + "\n\n".join(parts))

        if access:
            names = ", ".join(f"{a.kind}: {a.name}" for a in access)
            lines.append("# Доступы (отслеживаются отдельно, задач по ним НЕ создавай)\n"
                         + names)
        lines.append("Составь план работ одним JSON-объектом.")
        return "\n\n".join(lines)

    # ---------- вызов модели ----------

    async def _call(self, prompt: str, project_id: int) -> str:
        assert_no_secrets(prompt, self.vault)
        if self.client is None:
            raise PlanFailed(missing_secret_message("ANTHROPIC_API_KEY"))
        t0 = time.monotonic()
        resp = await self.client.messages.create(
            model=self.model, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        await self._charge(resp, time.monotonic() - t0, project_id)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    async def _charge(self, resp, seconds: float, project_id: int) -> None:
        from .verifier import PRICE_IN, PRICE_OUT
        usage = getattr(resp, "usage", None)
        cost = 0.0
        tokens_in = tokens_out = 0
        if usage is not None:
            tokens_in = getattr(usage, "input_tokens", 0)
            tokens_out = getattr(usage, "output_tokens", 0)
            cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
        async with Session() as s:
            t = Task(project_id=project_id, lane="chat", title="планирование",
                     status="done", cost_usd=cost, verify_class="human",
                     executor="manual")
            s.add(t)
            await s.flush()
            s.add(Run(task_id=t.id, kind="plan", ok=True, cost_usd=cost, seconds=seconds))
            p = await s.get(Project, project_id)
            if p is not None:
                p.cost_usd += cost
            await s.commit()
        log.info("план: вход %s токенов, выход %s токенов, $%.4f, %.1fs",
                 tokens_in, tokens_out, cost, seconds)

    # ---------- разбор и починка ответа ----------

    def validate(self, data) -> list[str]:
        errors = []
        if not isinstance(data, dict):
            return ["ответ не является JSON-объектом"]
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return ["tasks должен быть непустым списком"]
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                errors.append(f"tasks[{i}] должен быть объектом")
                continue
            if not str(t.get("title") or "").strip():
                errors.append(f"tasks[{i}].title обязателен")
            if not str(t.get("deliverable_ref") or "").strip():
                errors.append(f"tasks[{i}].deliverable_ref обязателен: "
                              f"укажи пункт ТЗ, из которого взялась задача")
            if t.get("verify_class") not in VERIFY_CLASSES:
                errors.append(f"tasks[{i}].verify_class должен быть одним из {VERIFY_CLASSES}")
            if t.get("executor") not in EXECUTORS:
                errors.append(f"tasks[{i}].executor должен быть одним из {EXECUTORS}")
            if not isinstance(t.get("acceptance"), list):
                errors.append(f"tasks[{i}].acceptance должен быть списком")
        return errors

    def normalize(self, tasks: list[dict], brief: dict,
                  access: list[AccessItem]) -> tuple[list[dict], list[str]]:
        """Приводит план к тому, что код готов исполнять, и объясняет правки."""
        deliverables = [i for i in (brief.get("deliverables") or []) if isinstance(i, dict)]
        others = [i for f in ("stack", "constraints", "assets") for i in (brief.get(f) or [])
                  if isinstance(i, dict)]
        known = deliverables + others
        access_names = [{"text": f"{a.kind} {a.name}"} for a in access]

        out: list[dict] = []
        notes: list[str] = []
        seen_titles: set[str] = set()

        for i, raw in enumerate(tasks):
            title = str(raw.get("title") or "").strip()
            ref = str(raw.get("deliverable_ref") or "").strip()

            # 1) происхождение: пункт ТЗ должен существовать
            probe = {"text": ref}
            match = next((item for item in known if same_item(item, probe)), None)
            if match is None:
                notes.append(f"«{title}»: выброшена — deliverable_ref «{ref}» "
                             f"не найден в ТЗ")
                continue

            # 2) доступы задачами не бывают: их ведёт чеклист
            if any(same_item(a, probe) for a in access_names) or _looks_like_access(title):
                notes.append(f"«{title}»: выброшена — это пункт чеклиста доступов, "
                             f"а не работа")
                continue

            if title.lower() in seen_titles:
                notes.append(f"«{title}»: выброшена — дубль по названию")
                continue
            seen_titles.add(title.lower())

            checks = valid_checks(raw.get("acceptance"))
            dropped = len(raw.get("acceptance") or []) - len(checks)
            if dropped:
                notes.append(f"«{title}»: {dropped} критери(ев) отброшено — "
                             f"неизвестный тип или нет обязательного поля")

            # 3) класс проверяемости решает код
            declared_class = raw.get("verify_class")
            real_class = classify({"acceptance": checks}, declared_class)
            if real_class != declared_class:
                notes.append(f"«{title}»: класс понижен {declared_class} -> {real_class} "
                             f"(детерминированных проверок нет)")

            # 4) исполнитель
            declared_exec = raw.get("executor")
            real_exec = pick_executor(raw, declared_exec)
            if real_exec != declared_exec:
                notes.append(f"«{title}»: исполнитель {declared_exec} -> {real_exec}")

            out.append({
                "title": title,
                "description": str(raw.get("description") or "").strip(),
                "deliverable_ref": item_text(match),
                "acceptance": checks,
                "verify_class": real_class,
                "executor": real_exec,
                "depends_on": [str(d).strip() for d in (raw.get("depends_on") or [])
                               if str(d).strip()],
                "estimate_min": int(raw.get("estimate_min") or 0),
                "risk": str(raw.get("risk") or "").strip(),
            })

        # зависимости на выброшенные задачи не имеют смысла
        titles = {t["title"] for t in out}
        for t in out:
            missing = [d for d in t["depends_on"] if d not in titles]
            if missing:
                notes.append(f"«{t['title']}»: зависимости не найдены и убраны: {missing}")
            t["depends_on"] = [d for d in t["depends_on"] if d in titles]
        return out, notes

    # ---------- главный вход ----------

    async def plan(self, project: Project) -> dict | None:
        brief = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
        if not brief or not brief.get("deliverables"):
            log.info("проект %s: планировать нечего — в брифе нет deliverables", project.id)
            return None

        async with Session() as s:
            access = (await s.execute(
                select(AccessItem).where(AccessItem.project_id == project.id))).scalars().all()

        prompt = self.build_prompt(project, brief, access)
        try:
            tasks = await self._attempt(prompt, project)
        except SecretLeak as e:
            log.critical("проект %s: %s — запрос НЕ отправлен", project.id, e)
            await self._escalate(project, f"планирование остановлено: {e}")
            return None
        except PlanFailed as e:
            log.error("проект %s: план не собран — %s", project.id, e)
            await self._escalate(project, f"план не собран: {e}")
            return None

        tasks, notes = self.normalize(tasks, brief, access)
        for note in notes:
            log.warning("проект %s: %s", project.id, note)
        if not tasks:
            await self._escalate(project, "план пуст: ни одна задача не прошла проверку")
            return None

        try:
            order = topological_order(tasks)
        except CycleInPlan as e:
            log.error("проект %s: %s", project.id, e)
            await self._escalate(project, str(e))
            return None
        rank = {title: i for i, title in enumerate(order)}
        tasks.sort(key=lambda t: rank.get(t["title"], 0))

        stats = autonomy(tasks)
        await self._persist(project, tasks, stats)
        if not stats["suitable"]:
            log.warning("проект %s: автономность %.0f%% ниже порога %.0f%% — "
                        "проект малопригоден для автопилота",
                        project.id, stats["auto_ratio"] * 100, cfg.autonomy_min_ratio * 100)
            await self._notify(project, stats)
        return {"tasks": tasks, "stats": stats, "notes": notes, "order": order}

    async def _attempt(self, prompt: str, project: Project) -> list[dict]:
        errors: list[str] = []
        current = prompt
        for attempt in range(1, cfg.plan_max_attempts + 1):
            raw = await self._call(current, project.id)
            cleaned = raw.replace("```json", "").replace("```", "").strip()
            try:
                data = json.loads(cleaned)
            except Exception:
                errors = ["ответ не разобрался как JSON"]
            else:
                errors = self.validate(data)
                if not errors:
                    return data["tasks"]
            log.warning("проект %s: план, попытка %s не прошла валидацию: %s",
                        project.id, attempt, "; ".join(errors)[:300])
            current = (prompt + "\n\n# Предыдущий ответ отвергнут, исправь:\n- "
                       + "\n- ".join(errors))
        raise PlanFailed("; ".join(errors)[:300])

    # ---------- запись ----------

    async def _persist(self, project: Project, tasks: list[dict], stats: dict) -> None:
        """Перепланирование не трогает сделанное.

        Выполненные задачи остаются как есть. Задача, чьё требование исчезло
        из ТЗ, помечается `orphaned` и показывается человеку — тот же принцип,
        что у брифа: молча ничего не пропадает.
        """
        async with Session() as s:
            existing = (await s.execute(
                select(Task).where(Task.project_id == project.id,
                                   Task.lane.in_(("build", "verify"))))).scalars().all()
            by_title = {t.title.strip().lower(): t for t in existing}
            fresh_titles = {t["title"].strip().lower() for t in tasks}

            for idx, spec in enumerate(tasks):
                key = spec["title"].strip().lower()
                row = by_title.get(key)
                if row is not None and row.status in ("done", "escalated"):
                    continue        # сделанное не трогаем никогда
                if row is None:
                    row = Task(project_id=project.id, lane="build", status="ready")
                    s.add(row)
                row.title = spec["title"]
                row.prompt = spec["description"]
                row.acceptance = spec["acceptance"]
                row.deliverable_ref = spec["deliverable_ref"]
                row.verify_class = spec["verify_class"]
                row.executor = spec["executor"]
                row.depends_on = spec["depends_on"]
                row.estimate_min = spec["estimate_min"]
                row.risk = spec["risk"]
                row.order_idx = idx
                row.orphaned = False
                row.updated_at = utcnow()

            for key, row in by_title.items():
                if key in fresh_titles or row.status in ("done", "escalated"):
                    continue
                # требование исчезло — задачу не удаляем, а показываем
                row.orphaned = True
                row.updated_at = utcnow()

            p = await s.get(Project, project.id)
            if p is not None:
                p.autonomy_ratio = stats["auto_ratio"]
                p.planned_at = utcnow()
                p.last_action = (f"план: {stats['tasks']} задач, "
                                 f"автономно {stats['auto_ratio'] * 100:.0f}%")
                p.updated_at = utcnow()
            await s.commit()
        log.info("проект %s: план записан — %s задач, автономность %.0f%%",
                 project.id, stats["tasks"], stats["auto_ratio"] * 100)

    async def _escalate(self, project: Project, text: str) -> None:
        async with Session() as s:
            p = await s.get(Project, project.id)
            if p is not None:
                p.status = "blocked"
                p.last_action = text[:300]
                p.updated_at = utcnow()
                await s.commit()
        await self._notify_text(project, f"Проект {project.id} «{project.title}»: {text}")

    async def _notify(self, project: Project, stats: dict) -> None:
        body = (f"Проект {project.id} «{project.title}»: автономно закрывается "
                f"{stats['auto_ratio'] * 100:.0f}% задач при пороге "
                f"{cfg.autonomy_min_ratio * 100:.0f}%.\n"
                f"Классы: {stats['by_class']}\nИсполнители: {stats['by_executor']}\n"
                f"Решай, браться ли за проект на автопилоте.")
        await self._notify_text(project, body)

    async def _notify_text(self, project: Project, body: str) -> None:
        if self.communicator is None:
            return
        notify = getattr(self.communicator, "notify_owner", None)
        if notify is not None:
            await notify(body)


def _looks_like_access(title: str) -> bool:
    low = (title or "").lower()
    return ("доступ" in low and any(w in low for w in ("получ", "запрос", "попрос"))) \
        or low.startswith("получить ")


async def buildable(task: Task, done_titles: set[str]) -> bool:
    """Можно ли отдать задачу в полосу build прямо сейчас."""
    if task.verify_class == "human" or task.executor != "claude_code":
        return False
    return all(d.strip().lower() in done_titles for d in (task.depends_on or []))
