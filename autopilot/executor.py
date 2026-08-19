"""Запуск Claude Code в headless-режиме на отдельной worktree проекта."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from sqlalchemy import select

from .config import cfg
from . import guard
from . import llm
from .db import (AccessItem, Project, Session, Task, close_run, open_run,
                 utcnow)
from .textenc import decode_console
from .vault import refs_in, to_env_names, vault

log = logging.getLogger("exec")

# Чем платит headless-сессия Claude Code. Подпиской, а не деньгами: `claude -p`
# ходит в то же пятичасовое окно, что и работа владельца руками, и записывать
# его расход в денежный счётчик значит врать в обе стороны — на балансе ничего
# не списано, а суточный бюджет встаёт.
#
# Тормоз при этом не потерян, а разделён надвое: `DAILY_BUDGET_USD` держит
# настоящие деньги (бриф и план, если вернуться на API), `DAILY_CLI_BUDGET_USD`
# — расход подписки. Полоса build встаёт от любого из двух.
RUN_BACKEND = "cli"

SYSTEM_RULES = """Ты работаешь автономно, человека рядом нет.
Правила:
- НЕ спрашивай подтверждений, принимай решения сам и фиксируй их в DECISIONS.md
- После работы обязательно проверь, что проект собирается
- Не трогай файлы вне текущего каталога
- НЕ КАЧАЙ SDK И ТУЛЧЕЙНЫ в рабочий каталог: ни Flutter, ни Android SDK,
  ни JDK, ни компиляторы. Они ставятся системно и должны быть уже на месте.
  Если нужного инструмента нет — так и напиши в отчёте и останови работу
  по этой задаче; её поставят руками. Однажды такая попытка раздула рабочий
  каталог до 3.7 ГБ и всё равно закончилась ничем.
  Ставить зависимости ПРОЕКТА (pip install в .venv, pub get) — можно и нужно
- Доступы лежат в переменных окружения ($ИМЯ). Бери их оттуда и НИКОГДА
  не печатай значения, не пиши их в файлы и не коммить
- В конце выведи короткий отчёт: что сделано, что не сделано, почему
"""


def _raw_prompt(task: Task, project: Project) -> str:
    parts = [SYSTEM_RULES, f"# Проект\n{project.title} (клиент: {project.client})"]
    if project.brief:
        parts.append("# ТЗ\n" + json.dumps(project.brief, ensure_ascii=False, indent=2))
    parts.append(f"# Задача\n{task.title}\n\n{task.prompt}")
    if task.acceptance:
        parts.append("# Критерии приёмки (должны пройти)\n" +
                     json.dumps(task.acceptance, ensure_ascii=False, indent=2))
    if task.defects:
        parts.append("# Дефекты предыдущей итерации — ИСПРАВЬ ИХ\n- " + "\n- ".join(task.defects))
    return "\n\n".join(parts)


def build_prompt(task: Task, project: Project) -> str:
    """Промпт, который реально уходит агенту.

    Значений секретов здесь нет и быть не может: ссылки {{SECRET:NAME}}
    превращаются в имена переменных окружения $NAME, а сами значения
    попадают в процесс через env= у subprocess.
    """
    return to_env_names(_raw_prompt(task, project))


def _why_failed(data: dict) -> str:
    """Почему сессия агента не удалась — словами, а не «None».

    CLI на исчерпании ходов отдаёт `is_error: true` и `result: null`, и в лог
    уезжало голое «None». Настоящая причина видна по соседним полям, и без
    неё непонятно, чинить промпт, поднимать потолок ходов или смотреть код.
    """
    turns = int(data.get("num_turns") or 0)
    stop = str(data.get("stop_reason") or "")
    result = data.get("result")
    if result:
        return vault.mask(str(result))[:500]
    if turns >= cfg.max_turns or stop == "tool_use":
        return (f"агент упёрся в потолок ходов: сделано {turns} при "
                f"MAX_TURNS={cfg.max_turns}, работа не закончена")
    return f"сессия оборвалась без объяснения (ходов {turns}, stop={stop!r})"


class Executor:
    async def run(self, task: Task, project: Project) -> dict:
        workspace = Path(project.workspace or cfg.workspaces / f"p{project.id}")
        # без каталога create_subprocess_exec падает с невнятным WinError 267
        workspace.mkdir(parents=True, exist_ok=True)
        raw = _raw_prompt(task, project)
        prompt = to_env_names(raw)
        env = await self._secret_env(raw, project)

        # ТОТ ЖЕ разбор пути, что у вызовов модели: npm на Windows ставит
        # `claude.CMD`, а `create_subprocess_exec` обёртки исполнять не умеет
        # и падает с «не найден файл», хотя файл на месте. В llm.py это
        # починили в фазе 9, а сюда правка не дошла — полосу build ни разу
        # не запускали живьём, и первый же настоящий прогон встал на этом:
        # три задачи подряд ушли в эскалацию, не начав работу.
        resolved, launcher = llm.resolve_cli(cfg.claude_bin)
        if not resolved:
            raise RuntimeError(
                f"не нашёл claude-бинарь {cfg.claude_bin!r} (CLAUDE_BIN): "
                f"Claude Code CLI не установлен или не в PATH")
        args = [*launcher, resolved, "-p"]
        if task.cc_session_id:
            # продолжаем ТОТ ЖЕ контекст — иначе на 3-й итерации агент забудет, что уже пробовал
            args += ["--resume", task.cc_session_id]
        args += [
            # ПРОМПТ ИДЁТ В STDIN, а не аргументом. Командная строка Windows
            # ограничена 32767 символами, а промпт исполнителя содержит весь
            # бриф: на проекте 8 это 56 тысяч символов. CreateProcess падает
            # с WinError 206, который Python отдаёт как FileNotFoundError, —
            # и первый живой прогон выглядел как «бинарь не найден», хотя
            # бинарь был на месте. Ровно ту же ловушку фаза 9 уже проходила
            # на вызовах модели, но до исполнителя правка не дошла.
            "--output-format", "json",
            "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
            "--permission-mode", "acceptEdits",
            "--max-turns", str(cfg.max_turns),
            "--model", cfg.cc_model,
        ]

        # Строка расхода — ДО запуска: сессия исполнителя живёт до получаса,
        # и всё это время её не было видно в учёте вообще
        run_id = await open_run(task.id, "execute")
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(workspace), env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            # Процесс не стартовал вовсе — работы не было, cost_usd=0
            spent = time.monotonic() - t0
            await close_run(run_id, ok=False, backend=RUN_BACKEND,
                            cost_usd=0.0, seconds=spent, estimated=True)
            # WinError 206 приходит сюда же, хотя означает совсем другое:
            # не «нет файла», а «слишком длинная командная строка»
            raise RuntimeError(f"не запустился {resolved!r}: {e}") from None

        # Дочерний claude не должен нас пережить: брошенный процесс дожигает
        # то же пятичасовое окно, что и работа владельца руками
        guard.watch_child(proc)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=cfg.task_timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            # Раньше здесь стояла оценка по времени — и она же поймала на
            # живых данных главный собственный промах: машина уснула на
            # середине задачи 514, время ожидания досчиталось до 7+6 часов,
            # и «оценка по времени» дала фантомные $30+ за прогон, который
            # не сделал ни одного хода. Полезной работы не было — cost_usd=0
            spent = time.monotonic() - t0
            await close_run(run_id, ok=False, backend=RUN_BACKEND,
                            cost_usd=0.0, seconds=spent, estimated=True)
            raise RuntimeError(f"timeout {cfg.task_timeout_sec}s") from None
        except BaseException:
            # отмена или сигнал: процесс убиваем сами, иначе он останется
            # работать сиротой
            if proc.returncode is None:
                proc.kill()
            raise
        finally:
            guard.forget_child(proc)

        elapsed = time.monotonic() - t0
        log_path = cfg.logs / f"task{task.id}_{int(time.time())}.json"
        # агент вполне может вывести пароль в отчёте — на диск он попасть не должен
        log_path.write_bytes(vault.mask_bytes(out or err or b""))

        try:
            # --output-format json у claude всегда настоящий UTF-8, но
            # decode_console не мешает: валидный UTF-8 побеждает первым
            data = json.loads(decode_console(out))
        except Exception:
            # Ответ не разобрать — цифры расхода нет. cost_usd=0: нечем
            # подтвердить, что квота вообще потрачена не сном, а работой
            await close_run(run_id, ok=False, backend=RUN_BACKEND,
                            cost_usd=0.0, seconds=elapsed, estimated=True,
                            log_path=str(log_path))
            # decode_console, а не голый UTF-8: ошибка запуска на этой
            # консоли (cp866) приходит не в UTF-8, и слепой decode
            # стирал текст причины в «������»
            tail = vault.mask(decode_console(err or out)[:500])
            raise RuntimeError(f"bad output: {tail!r}") from None

        cost = float(data.get("total_cost_usd") or 0)
        sid = data.get("session_id")

        if data.get("is_error"):
            # СТРОКУ ЗАКРЫВАЕМ ОБЯЗАТЕЛЬНО. На первом живом прогоне три сессии
            # упёрлись в потолок ходов, сожгли $9.39 квоты по счёту самого CLI
            # — и остались открытыми строками с нулём. Суточный потолок этих
            # денег не увидел, то есть тормоз опять не сработал там, где нужен.
            #
            # `cost` здесь — НАСТОЯЩАЯ цифра из ответа CLI, не наша оценка:
            # ответ пришёл, просто с ошибкой. Если CLI вообще не назвал
            # стоимость (cost == 0) — не гадаем по времени, а пишем 0.
            await close_run(run_id, ok=False, backend=RUN_BACKEND,
                            cost_usd=cost, seconds=elapsed, estimated=not cost,
                            log_path=str(log_path))
            raise RuntimeError(_why_failed(data)) from None

        await close_run(run_id, ok=True, backend=RUN_BACKEND, cost_usd=cost,
                        seconds=elapsed, log_path=str(log_path))
        async with Session() as s:
            t = await s.get(Task, task.id)
            if t is not None:
                t.cc_session_id = sid or t.cc_session_id
                t.cost_usd += cost
                t.updated_at = utcnow()
            p = await s.get(Project, project.id)
            if p is not None:
                p.cost_usd += cost
                p.last_action = f"код: {task.title}"
                p.updated_at = utcnow()
            await s.commit()

        log.info("task %s done in %.0fs / $%.3f", task.id, elapsed, cost)
        return data

    async def _secret_env(self, raw_prompt: str, project: Project) -> dict[str, str]:
        """Окружение процесса: своё + значения секретов проекта.

        Берём и то, на что ссылается сам промпт, и весь чеклист доступов —
        агенту может понадобиться доступ, явно в тексте не упомянутый.
        """
        async with Session() as s:
            item_refs = (await s.execute(
                select(AccessItem.secret_ref)
                .where(AccessItem.project_id == project.id,
                       AccessItem.secret_ref.isnot(None)))).scalars().all()
        wanted = list(dict.fromkeys(refs_in(raw_prompt) + [n for r in item_refs for n in refs_in(r)]))
        if not wanted:
            return None
        secrets = vault.env_for(*(("{{SECRET:%s}}" % n) for n in wanted))
        if not secrets:
            return None
        log.info("task-окружение: передаю %s секрет(ов) процессу, в промпт они не идут",
                 len(secrets))
        return {**os.environ, **secrets}
