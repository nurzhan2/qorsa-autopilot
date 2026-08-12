"""Три уровня проверки. Судья — отдельная модель, НЕ тот агент, что писал код."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from .config import cfg
from .db import Project, Run, Session, Task

log = logging.getLogger("verify")

JUDGE_PROMPT = """Ты приёмщик работы. Твоя задача — НАЙТИ расхождения с ТЗ, а не похвалить.
Исходи из того, что дефекты есть, пока не доказано обратное.

ТЗ задачи:
{criteria}

Диф изменений:
{diff}

Отчёт исполнителя (относись к нему скептически):
{report}

Верни СТРОГО JSON без markdown:
{{"verdict": "PASS" | "FAIL", "defects": ["конкретный дефект", ...]}}
"""

SHELL_TIMEOUT_SEC = 600

# Цены судьи за миллион токенов. Заданы руками: SDK их не отдаёт, а без них
# стоимость приёмки не попадает в суточный бюджет. Держи в актуальном состоянии.
PRICE_IN = float(os.getenv("JUDGE_PRICE_IN_USD_PER_MTOK", "3") or 3)
PRICE_OUT = float(os.getenv("JUDGE_PRICE_OUT_USD_PER_MTOK", "15") or 15)


class Verifier:
    def __init__(self):
        self.client = None
        # без ключа судья недоступен. По умолчанию это ДЕФЕКТ, а не «ну и ладно»:
        # молча пропускать llm-приёмку — прямое противоречие смыслу модуля.
        # JUDGE_OPTIONAL=1 понижает до пропуска с предупреждением.
        self.judge_optional = str(os.getenv("JUDGE_OPTIONAL", "")).strip().lower() in ("1", "true", "yes")
        if cfg.anthropic_key:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=cfg.anthropic_key)
        else:
            log.warning("ANTHROPIC_API_KEY пуст — llm-приёмка %s",
                        "пропускается" if self.judge_optional else "будет валить задачи")

    async def run(self, task: Task, project: Project) -> tuple[bool, list[str]]:
        cwd = project.workspace or str(cfg.workspaces / f"p{project.id}")
        defects: list[str] = []

        for check in task.acceptance or []:
            if not isinstance(check, dict):
                defects.append(f"кривой критерий приёмки: {check!r}")
                continue
            kind = check.get("type")
            try:
                if kind == "shell":
                    ok, msg = await self._shell(check["cmd"], cwd)
                elif kind == "http":
                    ok, msg = await self._http(check["url"], int(check.get("expect", 200)))
                elif kind == "llm":
                    ok, msg = await self._judge(check.get("criteria", ""), cwd, task, project)
                else:
                    log.warning("неизвестный тип проверки %r в задаче %s — пропускаю", kind, task.id)
                    ok, msg = True, ""
            except Exception as e:
                ok, msg = False, f"{kind}: {e}"
            if not ok:
                defects.append(msg)

        return (not defects), defects

    # --- уровень 1: детерминированные ---

    async def _shell(self, cmd: str, cwd: str) -> tuple[bool, str]:
        os.makedirs(cwd, exist_ok=True)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()                      # иначе процесс переживёт автопилот
            await proc.wait()
            return False, f"`{cmd}` не уложился в {SHELL_TIMEOUT_SEC}s"
        if proc.returncode == 0:
            return True, ""
        tail = (out or b"").decode(errors="replace")[-1200:]
        return False, f"`{cmd}` exit={proc.returncode}\n{tail}"

    async def _http(self, url: str, expect: int) -> tuple[bool, str]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code == expect:
            return True, ""
        return False, f"GET {url} -> {r.status_code}, ожидалось {expect}"

    # --- уровень 3: судья ---

    async def _judge(self, criteria: str, cwd: str, task: Task, project: Project) -> tuple[bool, str]:
        if not self.client:
            if self.judge_optional:
                log.warning("судья пропущен (нет ANTHROPIC_API_KEY), задача %s", task.id)
                return True, ""
            return False, "судья недоступен: не задан ANTHROPIC_API_KEY"

        diff = await self._diff(cwd)
        msg = JUDGE_PROMPT.format(
            criteria=criteria or task.title,
            diff=diff[:60000],
            report=task.last_error or "—",
        )
        t0 = time.monotonic()
        resp = await self.client.messages.create(
            model=cfg.judge_model, max_tokens=1500,
            messages=[{"role": "user", "content": msg}],
        )
        await self._charge(task, project, resp, time.monotonic() - t0)

        raw = "".join(b.text for b in resp.content if b.type == "text")
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(raw)
        except Exception:
            # судья сломался — не блокируем работу, но это видно в логах
            log.warning("судья вернул не-JSON для задачи %s: %r", task.id, raw[:300])
            return True, ""
        if data.get("verdict") == "PASS":
            return True, ""
        defects = [str(d) for d in data.get("defects", []) if str(d).strip()]
        return False, "судья: " + "; ".join(defects or ["не соответствует ТЗ"])

    async def _diff(self, cwd: str) -> str:
        """git diff без shell-специфики: `2>/dev/null ||` под Windows не работает."""
        for args in (["git", "--no-pager", "diff", "HEAD~1"], ["git", "--no-pager", "diff"]):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await proc.communicate()
            except (FileNotFoundError, NotADirectoryError):
                return ""
            if proc.returncode == 0 and out.strip():
                return out.decode(errors="replace")
        return ""

    async def _charge(self, task: Task, project: Project, resp, seconds: float) -> None:
        """Приёмка тоже стоит денег. Без этой записи суточный бюджет
        не видит расходов судьи и не срабатывает вовремя."""
        usage = getattr(resp, "usage", None)
        cost = 0.0
        if usage is not None:
            cost = (getattr(usage, "input_tokens", 0) / 1e6 * PRICE_IN
                    + getattr(usage, "output_tokens", 0) / 1e6 * PRICE_OUT)
        async with Session() as s:
            t = await s.get(Task, task.id)
            if t is not None:
                t.cost_usd += cost
            pr = await s.get(Project, project.id)
            if pr is not None:
                pr.cost_usd += cost
            s.add(Run(task_id=task.id, kind="judge", ok=True, cost_usd=cost, seconds=seconds))
            await s.commit()
