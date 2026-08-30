"""Связка агентов вокруг одной задачи: бриф → работа → приёмка → разбор.

Половина этой цепочки в проекте уже была: планировщик (`planner.py`) режет ТЗ
на задачи, исполнитель (`executor.py`) их делает, верификатор (`verifier.py`)
проверяет и решает — принято или на второй круг. Не хватало двух звеньев,
и обоих не хватало заметно.

**Разбор повторного провала.** Задача 531 («Веб-админка») четыре раза подряд
упала с ОДНИМ И ТЕМ ЖЕ дефектом: критерий ждал `302`, сервер отдавал `200`.
Каждый раз агент получал тот же промпт плюс строку «дефекты предыдущей
итерации», каждый раз чинил не то, и никто не спросил, почему цикл стоит
на месте. Четыре сессии по шесть минут и вся ночь работы заказа — в мусор.
`Diagnostician` включается, когда попытки начали повторяться, и отвечает
на один вопрос: почему не сходится и что делать иначе.

**Задание под конкретную задачу.** Исполнителю уходил статический шаблон плюс
описание из плана. Критерии приёмки он видел как JSON — то есть должен был
сам догадаться, что `http 302` на `/admin/` означает «без сессии редиректь
на логин». `Prompter` переводит критерии в требования словами.

ГЛАВНОЕ ОГРАНИЧЕНИЕ, и оно не обсуждается: **ни один агент отсюда не может
объявить задачу выполненной.** Это право только у детерминированных проверок
верификатора. Здесь модель отвечает на вопрос «почему не вышло и что делать
дальше», а не «всё ли хорошо». Иначе вся система превращается в то, против
чего написана: агент отчитывается «готово», модель соглашается, и никто
ничего не проверил.

Отсюда же и обращение с вердиктом `criterion_wrong`: код НЕ правит критерий
сам. Критерий — это и есть приёмка; молча переписать его, чтобы задача
прошла, значит подделать результат. Такая задача уходит владельцу с разбором.
"""
from __future__ import annotations

import json
import logging
import time

from .config import cfg
from . import llm
from .llm import LimitReached
from .db import Project, Session, Task, close_run, open_run, service_task, utcnow
from .verifier import parse_judge_json

log = logging.getLogger("crew")

# После скольких неудачных попыток звать разборщика. Раньше второй попытки
# смысла нет: первый провал — это нормальный ход цикла, агент ещё не видел
# дефекта. Стоять на месте цикл начинает со второго одинакового провала.
DIAGNOSE_AFTER = 2

VERDICTS = ("work_wrong", "criterion_wrong", "missing_tool", "unclear")

DIAGNOSE_SYSTEM = """Ты разбираешь ЗАСТРЯВШУЮ задачу автономного агента.

Агент уже несколько раз пытался её сделать и каждый раз проваливал приёмку.
Твоя работа — понять ПОЧЕМУ цикл стоит на месте, и сказать, что делать иначе.

Ты НЕ решаешь, выполнена ли задача. Это решают детерминированные проверки,
и твоё мнение их не заменяет. Не предлагай «считать задачу выполненной».

Ровно один вердикт:

  work_wrong      — критерий верен, работа ему не соответствует. Самый частый
                    случай. Скажи КОНКРЕТНО, что в коде не так и что сделать.
  criterion_wrong — работа верна, а критерий требует не того или недостижим
                    (например ждёт код ответа, которого при верном поведении
                    быть не может). Отвечай так, только если уверен: критерий
                    менять будет человек, а не ты.
  missing_tool    — не хватает инструмента, сервиса или доступа, и без него
                    проверка не пройдёт, что бы агент ни делал.
  unclear         — по имеющимся данным причину не назвать.

Правило, которое важнее остальных: если задание прямо требует поведения,
которого критерий и ждёт, — это work_wrong, даже если агент уверен в обратном.

Отвечай СТРОГО одним JSON-объектом без markdown:
{
  "verdict": "work_wrong|criterion_wrong|missing_tool|unclear",
  "why": "почему цикл не сходится, 1-3 предложения",
  "instruction": "что сделать в следующий заход, конкретно и по делу",
  "evidence": "на что в дефектах или задании ты опираешься"
}"""

PROMPT_SYSTEM = """Ты готовишь ЗАДАНИЕ для агента-разработчика.

У тебя есть описание задачи и её критерии приёмки — тот самый код, который
будет её проверять. Агент читает критерии как JSON и регулярно не понимает,
какого поведения от него хотят.

Переведи критерии в требования человеческим языком. Например `http` с
`expect: 302` на `/admin/` означает: без сессии эта страница обязана
редиректить на логин, а не отдавать содержимое.

Не придумывай требований, которых в задаче и критериях нет. Не предлагай
обойти или ослабить проверку: она и есть приёмка.

Отвечай коротким текстом без markdown-заголовков: сперва «Что должно
получиться», затем «Что именно проверят» списком."""


def _backend():
    """Бэкенд разбора. Тот же выбор, что у судьи (LLM_BACKEND_JUDGE):
    работа здесь такая же — смотреть на результат и объяснять его."""
    return llm.make("judge")


async def _ask(prompt: str, system: str, project_id: int, kind: str,
               backend=None, max_tokens: int = 2000) -> str:
    """Вызов модели со строкой расхода — как у планировщика и судьи.

    Разбор и подготовка задания тоже тратят окно подписки. Не записать их
    значит потерять их из учёта: суточный потолок перестанет видеть часть
    расхода ровно тогда, когда задача застряла и разборы пошли один за другим.
    """
    b = backend or _backend()
    task_id = await service_task(project_id, f"служебное: {kind}")
    run_id = await open_run(task_id, kind)
    t0 = time.monotonic()
    try:
        reply = await b.ask(prompt, system=system, max_tokens=max_tokens)
    except BaseException:
        await close_run(run_id, ok=False, backend=getattr(b, "name", "cli"),
                        cost_usd=0.0, seconds=time.monotonic() - t0,
                        estimated=True)
        raise
    await close_run(run_id, ok=True, backend=getattr(b, "name", "cli"),
                    cost_usd=float(getattr(reply, "cost_usd", 0) or 0),
                    seconds=time.monotonic() - t0)
    return reply.text or ""


def _criteria_text(task: Task) -> str:
    return json.dumps(task.acceptance or [], ensure_ascii=False, indent=2)


# ---------- звено 1: задание разработчику ----------

class Prompter:
    """Переводит критерии приёмки в требования словами.

    Работает ДО сборки и только когда есть что переводить: у задачи без
    критериев переводить нечего, а лишний вызов модели стоит окна подписки.
    """

    def __init__(self, backend=None):
        self.backend = backend

    async def brief_for(self, task: Task, project: Project) -> str | None:
        if not cfg.crew_prompter or not (task.acceptance or []):
            return None
        prompt = (f"# Задача\n{task.title}\n\n{task.prompt or ''}\n\n"
                  f"# Критерии приёмки (этим её будут проверять)\n"
                  f"{_criteria_text(task)}")
        try:
            text = await _ask(prompt, PROMPT_SYSTEM, project.id, "prompt",
                              backend=self.backend, max_tokens=1200)
        except LimitReached:
            # Упор в квоту — не провал работы, и глотать его здесь нельзя:
            # выше по стеку он останавливает все полосы разом
            raise
        except Exception as e:                      # noqa: BLE001
            # Не смогли подготовить задание — не повод не делать задачу.
            # Агент получит обычный промпт, как раньше. Ловим широко
            # намеренно: это подсказка, и уронить из-за неё сборку нельзя
            log.warning("задание для задачи %s не подготовлено: %s", task.id, e)
            return None
        return (text or "").strip() or None


# ---------- звено 2: разбор застрявшей задачи ----------

class Diagnostician:
    """Почему задача не сходится. НЕ решает, выполнена ли она."""

    def __init__(self, backend=None):
        self.backend = backend

    @staticmethod
    def should_run(task: Task) -> bool:
        """Пора ли разбирать. Первый провал разбирать нечего: агент ещё
        не видел дефекта и во второй заход, скорее всего, его починит."""
        return bool(cfg.crew_diagnose) and int(task.attempts or 0) >= DIAGNOSE_AFTER

    async def diagnose(self, task: Task, project: Project,
                       defects: list[str]) -> dict | None:
        prompt = (
            f"# Задача\n{task.title}\n\n{task.prompt or ''}\n\n"
            f"# Критерии приёмки\n{_criteria_text(task)}\n\n"
            f"# Попыток уже сделано: {task.attempts}\n"
            f"# Чем закончилась последняя\n- " + "\n- ".join(defects or ["—"]))
        try:
            raw = await _ask(prompt, DIAGNOSE_SYSTEM, project.id, "diagnose",
                             backend=self.backend, max_tokens=1500)
        except LimitReached:
            raise
        except Exception as e:                      # noqa: BLE001
            log.warning("разбор задачи %s не удался: %s", task.id, e)
            return None

        data, extracted = parse_judge_json(raw, require=("verdict",))
        if extracted:
            log.warning("задача %s: разбор начат прозой, JSON извлечён из текста",
                        task.id)
        if not isinstance(data, dict):
            log.warning("задача %s: разбор не разобрался как JSON", task.id)
            return None

        verdict = str(data.get("verdict") or "").strip()
        if verdict not in VERDICTS:
            # Неизвестный вердикт трактовать по-своему нельзя: «наверное,
            # он имел в виду work_wrong» — это ровно то гадание, из-за
            # которого приёмка однажды перестала что-либо значить
            log.warning("задача %s: разбор вернул неизвестный вердикт %r",
                        task.id, verdict)
            return None
        return {
            "verdict": verdict,
            "why": str(data.get("why") or "").strip(),
            "instruction": str(data.get("instruction") or "").strip(),
            "evidence": str(data.get("evidence") or "").strip(),
        }


def diagnosis_note(diag: dict) -> str:
    """Строка разбора для владельца и для лога."""
    names = {
        "work_wrong": "работа не соответствует критерию",
        "criterion_wrong": "критерий требует не того",
        "missing_tool": "не хватает инструмента или доступа",
        "unclear": "причину назвать не удалось",
    }
    head = names.get(diag["verdict"], diag["verdict"])
    parts = [f"разбор: {head}"]
    if diag.get("why"):
        parts.append(diag["why"])
    if diag.get("instruction"):
        parts.append(f"что делать: {diag['instruction']}")
    return " — ".join(parts)


def instruction_block(diag: dict | None) -> str:
    """Кусок промпта исполнителю с выводами разбора.

    Только для `work_wrong`: в остальных случаях чинить должен не агент.
    """
    if not diag or diag["verdict"] != "work_wrong":
        return ""
    lines = ["# РАЗБОР ПРЕДЫДУЩИХ ПОПЫТОК",
             "Ты уже пробовал эту задачу и проваливал приёмку. Разбор показал:"]
    if diag.get("why"):
        lines.append(f"- {diag['why']}")
    if diag.get("instruction"):
        lines.append(f"- СДЕЛАЙ ИНАЧЕ: {diag['instruction']}")
    lines.append("- Не обходи проверку и не ослабляй критерий: он и есть приёмка.")
    return "\n".join(lines)


# ---------- звено 3: чего задаче не хватает ----------

def supply_report(task: Task, project: Project) -> dict:
    """Что нужно этой задаче, чтобы её вообще можно было выполнить и принять.

    Инструменты берутся тем же кодом, что и у планировщика (`toolchain`),
    а доступы — из чеклиста проекта. Отдельного «агента-снабженца» тут нет
    намеренно: список нужного выводится из критериев ДЕТЕРМИНИРОВАННО,
    и спрашивать об этом модель значило бы менять факт на догадку.
    """
    from . import toolchain

    spec = {"title": task.title, "acceptance": task.acceptance or [],
            "description": task.prompt or ""}
    brief = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
    missing = toolchain.missing_tools([spec], brief)
    return {
        "task_id": task.id,
        "missing_tools": missing,
        "ready": not missing,
    }
