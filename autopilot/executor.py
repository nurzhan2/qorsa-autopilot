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
from . import autonomy
from . import breaker
from . import crew
from . import guard
from . import llm
from . import projectdir
from .db import (AccessItem, Project, Session, Task, attempts_of, close_run,
                 open_run, utcnow)
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
- НЕ ОСЛАБЛЯЙ ПРОВЕРКИ, КОТОРЫМИ ТЕБЯ ПРОВЕРЯЮТ. Тест, который назван
  в критерии приёмки, — это и есть приёмка. Переписать его так, чтобы он
  проходил, значит подделать результат: код видит, какие файлы ты изменил,
  и проверка, чей файл ты правил, доказательством считаться перестаёт.
  Не сходится тест — чини код или напиши в отчёте, что критерий неверен
- В конце выведи короткий отчёт: что сделано, что не сделано, почему
"""

# Сколько строк дефекта доходит до промпта. Режем СЕРЕДИНУ, а не хвост:
# у питоновского трейсбэка полезная часть — строка исключения — стоит В КОНЦЕ,
# и обрезка «первые N строк» срезала бы ровно её. Этот проект уже платил
# за обрезку входа по неверному признаку: 4 пункта ТЗ вместо 33 на живой
# переписке, потому что резали по количеству, а не по содержательности.
DEFECT_HEAD, DEFECT_TAIL = 4, 8


def _short(text: str) -> str:
    lines = str(text or "").splitlines()
    if len(lines) <= DEFECT_HEAD + DEFECT_TAIL:
        return str(text or "")
    cut = len(lines) - DEFECT_HEAD - DEFECT_TAIL
    return "\n".join([*lines[:DEFECT_HEAD], f"  … ({cut} строк вырезано) …",
                      *lines[-DEFECT_TAIL:]])


def _raw_prompt(task: Task, project: Project, brief: str | None = None,
                tried: str = "") -> str:
    parts = [SYSTEM_RULES, f"# Проект\n{project.title} (клиент: {project.client})"]
    if getattr(project, "autonomous_mode", False):
        # Без этого блока агент получает заглушку, видит отказ внешнего
        # сервиса и начинает её обходить: хардкодить «временный» ключ или
        # отключать проверку подписи. Обход дороже недоделки
        parts.append(autonomy.PROMPT_NOTE)
    if project.brief:
        parts.append("# ТЗ\n" + json.dumps(project.brief, ensure_ascii=False, indent=2))
    parts.append(f"# Задача\n{task.title}\n\n{task.prompt}")
    if task.acceptance:
        parts.append("# Критерии приёмки (должны пройти)\n" +
                     json.dumps(task.acceptance, ensure_ascii=False, indent=2))
    if task.defects:
        parts.append("# Дефекты предыдущей итерации — ИСПРАВЬ ИХ\n- " +
                     "\n- ".join(_short(d) for d in task.defects))
    # Что уже пробовали. Раньше агент видел дефекты ТОЛЬКО последней итерации
    # и не знал, что три захода назад ломился ровно туда же. Блок собирается
    # кодом из журнала попыток и не стоит ни одного вызова модели — в отличие
    # от разбора ниже, который платит окном подписки.
    #
    # Отдельным блоком, а не внутри разбора, намеренно: `instruction_block`
    # отдаётся только при вердикте `work_wrong`, и при `criterion_wrong`
    # история попыток до агента не доехала бы вовсе.
    if tried:
        parts.append(tried)
    # Выводы разбора застрявшей задачи. Задача 531 четыре раза подряд упала
    # с одним и тем же дефектом, получая каждый раз один и тот же промпт:
    # список дефектов без объяснения, почему предыдущая починка не сработала,
    # цикл со стены не сдвигает
    block = crew.instruction_block(task.diagnosis or None)
    if block:
        parts.append(block)
    if brief:
        parts.append("# Что от тебя хотят и чем это проверят\n" + brief)
    return "\n\n".join(parts)


def build_prompt(task: Task, project: Project, brief: str | None = None,
                 tried: str = "") -> str:
    """Промпт, который реально уходит агенту.

    Значений секретов здесь нет и быть не может: ссылки {{SECRET:NAME}}
    превращаются в имена переменных окружения $NAME, а сами значения
    попадают в процесс через env= у subprocess.
    """
    return to_env_names(_raw_prompt(task, project, brief, tried))


# Сессия CLI кончилась или её нет. Это состояние УСТАНОВКИ, а не задачи:
# чинится входом в claude, а не правкой кода. Живой случай: сбор ТЗ из
# дашборда встал на «OAuth session expired», и каждая следующая задача
# сожгла бы по попытке на том же самом.
AUTH_MARKERS = (
    "oauth session expired",
    "failed to authenticate",
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
)


def _looks_like_auth(text: str) -> bool:
    body = str(text or "").lower()
    return any(m in body for m in AUTH_MARKERS)


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
        # Каталог проекта, а не общий workspaces/: код заказа, его логи и его
        # ТЗ лежат вместе. Явно заданная колонкой «Папка» директория сильнее —
        # projectdir.workspace() это учитывает.
        # Без каталога create_subprocess_exec падает с невнятным WinError 267
        workspace = projectdir.workspace(project)
        # Задание словами: критерии приёмки агент читает как JSON и регулярно
        # не понимает, какого поведения от него хотят. Не собралось — работаем
        # как раньше, без него: это подсказка, а не условие работы
        brief = await crew.Prompter().brief_for(task, project)
        tried = await self._tried_block(task)
        raw = _raw_prompt(task, project, brief, tried)
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
        # Лог сессии — в каталоге проекта: в общем logs/ вывод сессий по
        # сорока задачам четырёх заказов лежал вперемешку и разбирался поиском
        log_path = projectdir.logs(project) / f"task{task.id}_{int(time.time())}.json"
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

            # УПОР В КВОТУ И ИСТЁКШАЯ СЕССИЯ — НЕ ПРОВАЛ РАБОТЫ.
            #
            # limits.py написан ровно про это, но правку до исполнителя
            # не донесли: здесь любой `is_error` уходил обычным RuntimeError
            # и в `_on_fail`, то есть тратил попытку. Четыре упора подряд —
            # и выполненная задача в эскалации, при том что с работой всё
            # в порядке. Ту же ошибку проект уже проходил в другом месте.
            #
            # Признак ищем ТОЛЬКО при отсутствии генерации: слова «rate
            # limit» в отчёте самой модели однажды стоили 26 минут работы,
            # выброшенной как «упор». Про исчерпание сообщает оболочка,
            # а не содержимое ответа.
            usage = data.get("usage") or {}
            produced = bool(cost) or int(usage.get("output_tokens") or 0) > 0
            reason = _why_failed(data)
            if not produced and llm.looks_like_limit(reason):
                raise llm.LimitReached(reason)
            if not produced and _looks_like_auth(reason):
                # То же самое, но чинится не ожиданием, а входом в CLI.
                # Гасим всю работу: у остальных задач будет ровно тот же
                # отказ, и каждая сожжёт по попытке впустую
                raise llm.LimitReached(
                    f"сессия Claude Code недействительна: {reason}. "
                    f"Войди заново: claude, затем /login")
            raise RuntimeError(reason) from None

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

    @staticmethod
    async def _tried_block(task: Task) -> str:
        """«Что уже пробовал — не повторяй», собранное кодом из журнала попыток.

        Подсказка, а не условие работы: не собралось — агент получает обычный
        промпт, как раньше. Ловим широко намеренно, по образцу `crew.Prompter`:
        уронить сборку из-за подсказки нельзя.
        """
        try:
            rows = await attempts_of(task.id, counted_only=True)
            return breaker.tried_block(
                [breaker.Attempt(n=r.n, lane=r.lane, defects=list(r.defects or []),
                                 sig=r.sig, note=r.note) for r in rows])
        except Exception as e:                          # noqa: BLE001
            log.warning("история попыток задачи %s не собрана: %s", task.id, e)
            return ""

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
