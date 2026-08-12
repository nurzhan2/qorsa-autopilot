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
from .db import AccessItem, Project, Run, Session, Task, utcnow
from .vault import refs_in, to_env_names, vault

log = logging.getLogger("exec")

SYSTEM_RULES = """Ты работаешь автономно, человека рядом нет.
Правила:
- НЕ спрашивай подтверждений, принимай решения сам и фиксируй их в DECISIONS.md
- После работы обязательно проверь, что проект собирается
- Не трогай файлы вне текущего каталога
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


class Executor:
    async def run(self, task: Task, project: Project) -> dict:
        workspace = Path(project.workspace or cfg.workspaces / f"p{project.id}")
        # без каталога create_subprocess_exec падает с невнятным WinError 267
        workspace.mkdir(parents=True, exist_ok=True)
        raw = _raw_prompt(task, project)
        prompt = to_env_names(raw)
        env = await self._secret_env(raw, project)

        args = [cfg.claude_bin, "-p"]
        if task.cc_session_id:
            # продолжаем ТОТ ЖЕ контекст — иначе на 3-й итерации агент забудет, что уже пробовал
            args += ["--resume", task.cc_session_id]
        args += [
            prompt,
            "--output-format", "json",
            "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep",
            "--permission-mode", "acceptEdits",
            "--max-turns", str(cfg.max_turns),
            "--model", cfg.cc_model,
        ]

        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(workspace), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(f"не нашёл claude-бинарь {cfg.claude_bin!r} (CLAUDE_BIN)") from None

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.task_timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"timeout {cfg.task_timeout_sec}s") from None

        elapsed = time.monotonic() - t0
        log_path = cfg.logs / f"task{task.id}_{int(time.time())}.json"
        # агент вполне может вывести пароль в отчёте — на диск он попасть не должен
        log_path.write_bytes(vault.mask_bytes(out or err or b""))

        try:
            data = json.loads(out.decode(errors="replace"))
        except Exception:
            tail = vault.mask((err or out or b"").decode(errors="replace")[:500])
            raise RuntimeError(f"bad output: {tail!r}") from None

        if data.get("is_error"):
            raise RuntimeError(vault.mask(str(data.get("result")))[:500])

        cost = float(data.get("total_cost_usd") or 0)
        sid = data.get("session_id")

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
            s.add(Run(task_id=task.id, kind="execute", ok=True,
                      cost_usd=cost, seconds=elapsed, log_path=str(log_path)))
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
