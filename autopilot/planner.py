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
import re
import time

from sqlalchemy import select

from .brief import (BriefFailed, SecretLeak, assert_no_secrets, item_text,
                    same_item)
from . import checks
from . import guard
from . import llm
from .config import cfg
from pathlib import Path

from .db import (AccessItem, Project, Session, Task, close_run, open_run,
                 utcnow)
from .vault import anthropic_key, missing_secret_message
from .verifier import parse_judge_json
from .vault import vault as default_vault

log = logging.getLogger("plan")

VERIFY_CLASSES = ("auto", "assisted", "human")
EXECUTORS = ("claude_code", "manual", "external")

# Типы проверок берутся из общего реестра. Планировщик не вправе выдать тип,
# которого верификатор не умеет, — именно на этом разрыве приёмка однажды
# проходила вообще без проверок.
DETERMINISTIC_CHECKS = checks.DETERMINISTIC
ASSISTED_CHECKS = checks.ASSISTED
KNOWN_CHECKS = checks.KNOWN

# Признаки того, что работа лежит вне репозитория и агенту недоступна
MANUAL_HINTS = (
    "админк", "панел", "вручную", "руками", "личный кабинет", "интерфейс",
    "зарегистрир", "оплат", "счётчик метрики", "счетчик метрики",
    "загрузить через", "залить через", "настроить в кабинете",
)

SYSTEM_TEMPLATE = """Ты технический директор. Из готового ТЗ ты собираешь план работ.

Твоя главная задача — НЕ придумать красивые задачи, а честно сказать, что
из этого можно проверить машиной, а что нельзя.

ЖЕЛЕЗНЫЕ ПРАВИЛА:

1. Каждая задача происходит из пункта ТЗ. В deliverable_ref пиши ТОЧНЫЙ текст
   пункта, из которого задача взялась. Задача без deliverable_ref будет
   выброшена: план не должен содержать работы, которую никто не просил.

2. acceptance — это КОД, который выполнится, а не пожелание. Только эти типы,
   другие верификатор исполнить не сможет:
{CHECKS}
   Не выдумывай проверку, которой не будет. «Сайт работает» — это не проверка.

   ГЛАВНОЕ ПРАВИЛО КРИТЕРИЯ: он обязан ПАДАТЬ на состоянии «эта задача не
   выполнена, всё остальное на месте». Критерий, который проходит на пустом
   проекте, бесполезен.
     ПЛОХО: задача «установить WooCommerce», критерий http https://site/ → 200
            (главная отдаёт 200 и без WooCommerce — критерий пройдёт всегда)
     ХОРОШО: dom https://site/cart/ селектор .woocommerce-cart
            (на сайте без WooCommerce страницы корзины нет)
   Проверяй каждый критерий этим вопросом: «а если задачу не делать, он
   упадёт?». Если нет — критерий негодный, придумай другой.

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

7. СТЕК ВЫБИРАЕШЬ ТЫ, но обосновываешь ОГРАНИЧЕНИЯМИ, а не вкусом.
   Если в ТЗ стек указан — планируй по нему и не подменяй, в stack_decision
   так и напиши: «взят из ТЗ».

   Если не указан — правило по умолчанию: САМОЕ ПРОСТОЕ РЕШЕНИЕ, закрывающее
   требования. Монолит, одна база, готовые сервисы вместо своей инфраструктуры.

   Микросервисы, отдельный кэш-слой, очереди сообщений, свой API Gateway,
   Kubernetes и прочая тяжёлая инфраструктура появляются ТОЛЬКО если в ТЗ
   есть цифры, которые их требуют: тысячи запросов в секунду, десятки тысяч
   заказов в день, несколько команд разработки, требование по отказоустойчивости.
   Пятьдесят заказов в день, один город и две недели срока — это монолит,
   и никакие «на вырост» этого не меняют: за архитектуру на вырост платит
   клиент, а пользуется ей никто.

   Смотри на РАМКИ ПРОЕКТА (срок и бюджет ниже, если они известны). Решение,
   которое в них не помещается, — неправильное решение, каким бы красивым
   оно ни было.

Отвечай СТРОГО одним JSON-объектом без markdown:
{
  "stack_decision": {
    "chosen": ["конкретные технологии списком"],
    "rationale": "почему именно так, со ссылкой на ограничения проекта",
    "driven_by": ["срок 2 недели", "бюджет 150 тысяч", "50 заказов в день"],
    "rejected": [{"option": "что рассматривалось и отброшено",
                  "why": "на каком основании"}]
  },
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

SYSTEM = SYSTEM_TEMPLATE.replace("{CHECKS}", checks.prompt_reference())


class PlanFailed(RuntimeError):
    """Модель не смогла отдать валидный план."""


class CycleInPlan(RuntimeError):
    """Граф зависимостей зациклен. Разруливать это гаданием нельзя."""


# ---------- проверки, которые делает КОД ----------

valid_checks = checks.valid_checks


def split_ref(ref: str) -> list[str]:
    """Разбирает составную ссылку на пункты ТЗ.

    Модель просят дать ТОЧНЫЙ текст одного пункта, но она регулярно
    перечисляет несколько через «;» и дописывает модальность в скобках:

        «Клиентское приложение (must); Админ-панель (must)»

    Целиком такая строка не совпадает ни с чем, и задача выбрасывалась как
    выдуманная. На живом плане так пропали инициализация репозитория и
    E2E-тестирование — а потом код снял зависимости на несуществующие задачи,
    и граф разъехался у шести штук.

    Возвращает части в исходном порядке, без модальности и пустых.
    """
    body = str(ref or "")
    parts = []
    for chunk in re.split(r"[;\n]+|(?<=\))\s*\+\s*", body):
        # хвост вида «(must)», «(should)», «(nice)» — это модальность,
        # а не часть формулировки пункта
        clean = re.sub(r"\s*\((?:must|should|nice)\)\s*$", "", chunk.strip(),
                       flags=re.IGNORECASE).strip(" .;")
        if clean:
            parts.append(clean)
    return parts if len(parts) > 1 else []


def constraints_block(brief: dict) -> str:
    """Рамки проекта для промпта: срок, бюджет, ожидаемая нагрузка.

    Без них планировщик выбирает решение в вакууме — и выбирает дорогое.
    На живом проекте отсутствие двух строчек («две недели», «150 тысяч»)
    дало микросервисы за API Gateway с PostgreSQL и Redis на приложение
    для одного города с полусотней заказов в день.
    """
    import datetime as _dt

    today = _dt.date.today()
    rows: list[str] = []
    for field, label in (("deadline", "Срок"), ("budget", "Бюджет")):
        item = brief.get(field)
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            row = f"* {label}: {item['text']}"
            if field == "deadline" and item.get("date"):
                # Считаем остаток КОДОМ. Модель не знает сегодняшнего числа и
                # уже ошиблась на год: «сентябрь 2026» она прочитала как
                # «через 14 месяцев», хотя до него две недели. От этой цифры
                # зависит выбор решения, гадать её нельзя
                try:
                    left = (_dt.date.fromisoformat(str(item["date"])[:10]) - today).days
                    row += (f"  (это {left} дней от сегодня)" if left >= 0
                            else f"  (СРОК УЖЕ ПРОШЁЛ {abs(left)} дней назад)")
                except ValueError:
                    pass
            rows.append(row)

    # нагрузка обычно спрятана в constraints — вытаскиваем её явно
    for item in (brief.get("constraints") or []):
        text = item_text(item) if isinstance(item, dict) else ""
        if any(w in text.lower() for w in ("заказ", "польз", "нагруз", "трафик", "в день")):
            rows.append(f"* Нагрузка: {text}")

    if not rows:
        return ""
    # Сегодняшнее число — первой строкой и только если рамки вообще есть:
    # без него «к сентябрю» превращается в срок наугад
    return ("# РАМКИ ПРОЕКТА (решение обязано поместиться сюда)\n"
            + f"* Сегодня: {today.isoformat()}\n"
            + "\n".join(rows)
            + "\n\nЭто не требования и не задачи — это границы, внутри которых "
              "выбирается решение. Архитектура, которая в них не помещается, "
              "неправильная, какой бы правильной она ни выглядела вообще.")


def stack_declared(brief: dict) -> bool:
    """Зафиксирован ли стек в ТЗ.

    Пустой список — это не «любой», а «не обсуждали». Разница принципиальная:
    в первом случае можно брать что угодно, во втором нельзя брать ничего,
    пока не спросишь.
    """
    return any(str(item_text(i)).strip()
               for i in (brief.get("stack") or []) if isinstance(i, dict))


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
    """Доля работы, которую система закроет без человека.

    Две цифры, и они говорят разное. По ЧИСЛУ задач пятнадцать мелких ручных
    против одной большой автоматической дают 7%, хотя по времени картина
    может быть обратной. Поэтому считаем и по количеству, и по estimate_min.
    """
    total = len(tasks) or 1
    minutes_total = sum(int(t.get("estimate_min") or 0) for t in tasks)
    by_class = {c: sum(1 for t in tasks if t["verify_class"] == c) for c in VERIFY_CLASSES}
    by_executor = {e: sum(1 for t in tasks if t["executor"] == e) for e in EXECUTORS}

    def is_autonomous(t: dict) -> bool:
        # автономно закрывается только то, что агент и сделает, и проверит
        return t["verify_class"] == "auto" and t["executor"] == "claude_code"

    autonomous = sum(1 for t in tasks if is_autonomous(t))
    minutes_auto = sum(int(t.get("estimate_min") or 0) for t in tasks if is_autonomous(t))
    ratio = autonomous / total
    ratio_time = (minutes_auto / minutes_total) if minutes_total else 0.0
    return {
        "tasks": len(tasks),
        # именно автономные, а не by_class["auto"]: задача может быть
        # проверяемой машиной, но выполняться руками в чужой админке
        "autonomous": autonomous,
        "minutes": minutes_total,
        "minutes_auto": minutes_auto,
        "by_class": by_class,
        "by_executor": by_executor,
        "auto_ratio": round(ratio, 2),
        "auto_ratio_time": round(ratio_time, 2),
        # берём лучшую из двух: если по времени автопилот закрывает половину,
        # проект имеет смысл, даже когда мелких ручных задач много
        "suitable": max(ratio, ratio_time) >= cfg.autonomy_min_ratio,
    }


class Planner:
    def __init__(self, client=None, vault=None, communicator=None, model: str | None = None,
                 backend=None):
        self._client = client
        self._backend = backend
        self.vault = vault or default_vault
        self.communicator = communicator
        self.model = model or cfg.plan_model

    @property
    def backend(self):
        """Откуда берём ответ: API или CLI. Задаётся LLM_BACKEND_PLAN."""
        if self._backend is None:
            self._backend = llm.make("plan", client=self._client)
        return self._backend

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

        frame = constraints_block(brief)
        if frame:
            # Рамки идут ПОСЛЕ ТЗ: это не требование, а то, внутри чего решение
            # обязано поместиться. Без них планировщик предлагал микросервисы
            # на приложение с полусотней заказов в день и двумя неделями срока
            lines.append(frame)

        if access:
            names = ", ".join(f"{a.kind}: {a.name}" for a in access)
            lines.append("# Доступы (отслеживаются отдельно, задач по ним НЕ создавай)\n"
                         + names)
        lines.append("Составь план работ одним JSON-объектом.")
        return "\n\n".join(lines)

    # ---------- вызов модели ----------

    async def _call(self, prompt: str, project_id: int) -> tuple[str, str]:
        assert_no_secrets(prompt, self.vault)
        # Строка расхода заводится ДО вызова: план на подписке идёт больше
        # десяти минут, и всё это время в учёте не было ничего. Прогон,
        # убитый на середине, не оставлял следа вовсе — квоту сжёг,
        # а показать нечего
        task_id = await self._service_task(project_id)
        run_id = await open_run(task_id, "plan")
        t0 = time.monotonic()
        try:
            reply = await self.backend.ask(prompt, system=SYSTEM, model=self.model,
                                           max_tokens=cfg.plan_max_tokens)
        except BaseException:
            await close_run(run_id, ok=False,
                            backend=getattr(self.backend, "name", "api"),
                            cost_usd=0.0, seconds=time.monotonic() - t0)
            raise
        await self._charge(reply, project_id, task_id, run_id)
        return reply.text, reply.stop_reason

    async def _service_task(self, project_id: int) -> int:
        """Служебная задача, к которой привязан расход: Run без task_id нет."""
        async with Session() as s:
            t = Task(project_id=project_id, lane="chat", title="планирование",
                     status="done")
            s.add(t)
            await s.commit()
            return t.id

    async def _charge(self, reply, project_id: int, task_id: int, run_id: int) -> None:
        """Расход. У CLI это оценка подписки, а не деньги — см. llm.Reply."""
        money = ("$%.4f" % reply.cost_usd if reply.billed
                 else "~$%.4f (подписка)" % reply.cost_usd)
        log.info("план [%s]: вход %s токенов, выход %s, %s, %.1fs",
                 reply.backend, reply.input_tokens, reply.output_tokens,
                 money, reply.seconds)
        await close_run(run_id, ok=True, backend=reply.backend,
                        cost_usd=reply.cost_usd, seconds=reply.seconds)
        async with Session() as s:
            t = await s.get(Task, task_id)
            if t is not None and reply.billed:
                t.cost_usd += reply.cost_usd
            p = await s.get(Project, project_id)
            if p is not None and reply.billed:
                p.cost_usd += reply.cost_usd
            await s.commit()

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
        # ЦЕЛЬ — тоже законное происхождение задачи.
        #
        # Раньше её тут не было, и на живом плане это выбросило подготовку
        # к публикации в App Store и Google Play — то есть то, чем проект
        # заканчивается. Цель прямо про публикацию и говорила. Правило против
        # выдуманной работы остаётся: ссылка обязана на что-то указывать.
        # Но цель — не выдумка, а самая главная строчка ТЗ.
        goal = brief.get("goal") if isinstance(brief.get("goal"), dict) else None
        goal_items = [goal] if goal and str(goal.get("text") or "").strip() else []
        known = deliverables + others + goal_items
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
                # Модель регулярно склеивает несколько пунктов в одну ссылку:
                # «Клиентское приложение (must); Админ-панель (must)». Целиком
                # такая строка не совпадает ни с чем, и задача выбрасывалась —
                # на живом плане так потерялись инициализация репозитория
                # и E2E-тестирование, а следом разъехался граф зависимостей.
                parts = split_ref(ref)
                for part in parts:
                    probe_part = {"text": part}
                    match = next((i for i in known if same_item(i, probe_part)), None)
                    if match is not None:
                        probe = probe_part
                        notes.append(f"«{title}»: ссылка склеена из {len(parts)} "
                                     f"пунктов, взят первый совпавший — «{part}»")
                        break
            if match is None:
                notes.append(f"«{title}»: выброшена — deliverable_ref «{ref}» "
                             f"не найден в ТЗ")
                continue
            # откуда именно взялась задача. Ссылка на цель законна, но это
            # более слабое основание, чем пункт: цель широкая, и под неё легко
            # подвести что угодно. Поэтому такие задачи показываются отдельно
            origin = ("goal" if goal_items and match is goal_items[0]
                      else "deliverable")

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
                "ref_origin": origin,
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
        """Собрать план. Один пишущий процесс на проект — второй не начнётся.

        Замок стоит здесь, а не в скрипте: писать задачи через `_persist`
        может кто угодно — `plan_eval`, планировщик полос, будущий вызов
        из главного цикла. Два прогона на проекте 8 уже перемешали план,
        и разбирать это пришлось руками.
        """
        brief = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
        if not brief or not brief.get("deliverables"):
            log.info("проект %s: планировать нечего — в брифе нет deliverables", project.id)
            return None

        with guard.project_lock(project.id, "планирование"):
            return await self._plan_locked(project, brief)

    async def _plan_locked(self, project: Project, brief: dict) -> dict | None:
        async with Session() as s:
            access = (await s.execute(
                select(AccessItem).where(AccessItem.project_id == project.id))).scalars().all()

        prompt = self.build_prompt(project, brief, access)
        try:
            tasks, decision = await self._attempt(prompt, project)
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

        if stack_declared(brief) and decision:
            decision = dict(decision)
            decision.setdefault("rationale", "")
            decision["from_brief"] = True
        decisions_path = await self._write_decisions(project, decision)

        stats = autonomy(tasks)
        await self._persist(project, tasks, stats)
        if not stats["suitable"]:
            log.warning("проект %s: автономность %.0f%% ниже порога %.0f%% — "
                        "проект малопригоден для автопилота",
                        project.id, stats["auto_ratio"] * 100, cfg.autonomy_min_ratio * 100)
            await self._notify(project, stats)
        return {"tasks": tasks, "stats": stats, "notes": notes, "order": order,
                "stack_decision": decision, "decisions_path": decisions_path}

    async def _attempt(self, prompt: str, project: Project) -> tuple[list[dict], dict]:
        errors: list[str] = []
        current = prompt
        for attempt in range(1, cfg.plan_max_attempts + 1):
            raw, stop_reason = await self._call(current, project.id)
            # тот же терпимый разбор, что у судьи и у брифа: модель регулярно
            # предваряет JSON прозой, и строгий json.loads давал ложный провал
            # require: в прозе перед планом попадается свой маленький объект,
            # и без опознавательного ключа вынули бы именно его — на живом
            # прогоне это стоило попытки и тринадцати минут
            data, extracted = parse_judge_json(raw, require=("tasks",))
            if extracted:
                log.warning("проект %s: план начат прозой, JSON извлечён из текста",
                            project.id)
            if data is None:
                if stop_reason == "max_tokens":
                    # На 32 пунктах ТЗ план в 8000 токенов не помещался, и
                    # обрыв выглядел как «модель отдала мусор». Это враньё:
                    # JSON был правильный, он просто не дописался
                    errors = [f"ответ не поместился в {cfg.plan_max_tokens} токенов "
                              f"и оборвался — подними PLAN_MAX_TOKENS"]
                    log.error("проект %s: ответ планировщика обрезан по лимиту",
                              project.id)
                else:
                    errors = ["ответ не разобрался как JSON"]
            else:
                errors = self.validate(data)
                if not errors:
                    decision = data.get("stack_decision")
                    return data["tasks"], (decision if isinstance(decision, dict) else {})
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
                p.autonomy_ratio_time = stats["auto_ratio_time"]
                p.planned_at = utcnow()
                p.last_action = (f"план: {stats['tasks']} задач, автономно "
                                 f"{stats['auto_ratio'] * 100:.0f}% задач / "
                                 f"{stats['auto_ratio_time'] * 100:.0f}% времени")
                p.updated_at = utcnow()
            await s.commit()
        log.info("проект %s: план записан — %s задач, автономность %.0f%%",
                 project.id, stats["tasks"], stats["auto_ratio"] * 100)

    async def _write_decisions(self, project: Project, decision: dict) -> str | None:
        """Решение по стеку — отдельным артефактом, который переживёт сессию.

        Через месяц вопрос «почему тут React Native, а не Flutter» возникнет
        обязательно, и ответ на него должен лежать в репозитории проекта,
        а не в логе давно закрытого прогона.
        """
        if not decision:
            return None
        root = Path(project.workspace or (cfg.workspaces / f"p{project.id}"))
        path = root / "docs" / "DECISIONS.md"
        chosen = decision.get("chosen") or []
        driven = decision.get("driven_by") or []
        rejected = decision.get("rejected") or []

        lines = [
            "# Технические решения",
            "",
            f"Проект: {project.title}",
            f"Записано: {utcnow().date().isoformat()}",
            "",
            "## Стек",
            "",
        ]
        lines += [f"* {c}" for c in chosen] or ["* (не выбран)"]
        lines += ["", "## Почему так", "", str(decision.get("rationale") or "—"), ""]
        if driven:
            lines += ["## Чем продиктовано", ""]
            lines += [f"* {d}" for d in driven] + [""]
        if rejected:
            lines += ["## Что отброшено и почему", ""]
            for item in rejected:
                if isinstance(item, dict):
                    lines.append(f"* **{item.get('option')}** — {item.get('why')}")
                else:
                    lines.append(f"* {item}")
            lines.append("")
        lines += [
            "---",
            "",
            "Решение принято планировщиком из ограничений проекта (срок, бюджет,",
            "нагрузка, число платформ), а не из предпочтений. Если ограничения",
            "изменились — перепланируй, файл перезапишется.",
            "",
        ]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            log.exception("не смог записать %s", path)
            return None
        log.info("проект %s: решение по стеку записано в %s", project.id, path)
        return str(path)

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
                f"{stats['auto_ratio'] * 100:.0f}% задач и "
                f"{stats['auto_ratio_time'] * 100:.0f}% времени при пороге "
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
