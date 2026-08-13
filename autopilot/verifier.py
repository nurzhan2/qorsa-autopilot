"""Три уровня проверки. Судья — отдельная модель, НЕ тот агент, что писал код."""
from __future__ import annotations

import asyncio
import base64
import dataclasses as dc
import json
import logging
import os
import time
from pathlib import Path

import httpx

from .checks import (DEFAULT_TIMEOUT_SEC, HUMAN_ONLY, REGISTRY, VIEWPORTS,
                     is_runnable, spec)
from .config import cfg
from .db import Project, Run, Session, Task
from .vault import anthropic_key, missing_secret_message

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


@dc.dataclass(frozen=True)
class Verdict:
    """Итог приёмки. Важно не только «прошло», но и ЧЕМ прошло.

    `screenshot` и `llm` мы сами называем подсказкой: модель посмотрела на
    картинку и сказала, что похоже на описанное. До этой правки такой вердикт
    попадал в статус задачи неотличимо от зелёного теста — то есть оценка
    модели по скриншоту приравнивалась к `npm run build` с кодом возврата 0.
    Это неправда, и это ровно тот молчаливый пропуск, ради которого затевалась
    вся фаза: задача выглядела принятой, хотя ничего детерминированного её
    не проверяло.

    Поэтому счётчики раздельные, а решение о статусе принимает вызывающий код
    по двум свойствам ниже. Сам верификатор статусов не ставит: он сообщает
    факты, последнее слово — за кодом, который эти факты читает.
    """

    ok: bool
    defects: list[str]
    # сколько проверок РЕАЛЬНО исполнено, по видам вердикта
    deterministic: int = 0      # shell / http / file_exists / dom — судит код
    assisted: int = 0           # screenshot / llm — судит модель

    @property
    def confirmed(self) -> bool:
        """Прошла так, что машине можно верить: есть хоть одна проверка,
        вердикт которой вынес код, а не модель. Только это закрывает задачу
        автоматически."""
        return self.ok and self.deterministic > 0

    @property
    def assisted_only(self) -> bool:
        """Прошла, но целиком на слово модели. Не провал и не приёмка —
        задача уходит владельцу на подтверждение."""
        return self.ok and self.deterministic == 0 and self.assisted > 0

    def summary(self) -> str:
        return (f"детерминированных проверок: {self.deterministic}, "
                f"с моделью: {self.assisted}")

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
        key = anthropic_key()
        if key:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=key)
        else:
            log.warning("ключа нет — llm-приёмка %s.\n%s",
                        "пропускается" if self.judge_optional else "будет валить задачи",
                        missing_secret_message("ANTHROPIC_API_KEY"))

    # Тип проверки -> метод. Покрытие реестра проверяется тестом:
    # разрыв между тем, что планировщик выдаёт, и тем, что верификатор
    # умеет, однажды уже дал «приёмку без единой проверки».
    @property
    def handlers(self) -> dict:
        return {
            "shell": self._check_shell,
            "http": self._check_http,
            "file_exists": self._check_file_exists,
            "dom": self._check_dom,
            "screenshot": self._check_screenshot,
            "llm": self._check_llm,
            "human": self._check_human,
        }

    async def run(self, task: Task, project: Project) -> Verdict:
        cwd = project.workspace or str(cfg.workspaces / f"p{project.id}")
        defects: list[str] = []
        executed = 0
        # считаем раздельно: кто вынес вердикт — код или модель
        deterministic = 0
        assisted = 0

        for check in task.acceptance or []:
            if not isinstance(check, dict):
                defects.append(f"кривой критерий приёмки: {check!r}")
                continue
            kind = str(check.get("type") or "")
            handler = self.handlers.get(kind)
            if handler is None:
                # Раньше здесь стоял пропуск, и задача с одними неизвестными
                # критериями объявлялась принятой. Молчаливый пропуск проверки —
                # худший режим этой системы: он выглядит как успех
                defects.append(f"неизвестный тип проверки {kind!r} — приёмка невозможна")
                continue
            if not is_runnable(check):
                if kind in HUMAN_ONLY:
                    defects.append(f"human: {check.get('criteria') or 'нужен человек'}")
                else:
                    missing = [f for f in REGISTRY[kind].required
                               if not str(check.get(f) or "").strip()]
                    defects.append(f"{kind}: нет обязательных полей {missing} — "
                                   f"проверка не запустится")
                continue

            try:
                ok, msg = await handler(check, cwd, task, project)
            except Exception as e:
                ok, msg = False, f"{kind}: {e}"
            executed += 1
            # проверка исполнена — записываем, чьим вердиктом она закрыта.
            # Считаем даже упавшую: счётчик отвечает на вопрос «чем эту задачу
            # вообще проверяют», а не «что прошло»
            s = spec(kind)
            if s is not None and s.deterministic:
                deterministic += 1
            elif s is not None and s.assisted:
                assisted += 1
            if not ok:
                defects.append(msg)

        if executed == 0:
            # Ни одной исполнимой проверки. Это не «нечего проверять»,
            # это «проверить нечем» — и оно не может считаться приёмкой
            defects.append("нет ни одной исполнимой проверки — принять задачу нечем")

        return Verdict(ok=not defects, defects=defects,
                       deterministic=deterministic, assisted=assisted)

    # --- обёртки под единый контракт (check, cwd, task, project) ---

    async def _check_shell(self, check, cwd, task, project):
        return await self._shell(check["cmd"], cwd)

    async def _check_http(self, check, cwd, task, project):
        return await self._http(check["url"], int(check.get("expect", 200) or 200))

    async def _check_llm(self, check, cwd, task, project):
        return await self._judge(check.get("criteria", ""), cwd, task, project)

    async def _check_human(self, check, cwd, task, project):
        """Человеческая проверка машиной не выполняется — и не притворяется."""
        return False, f"human: {check.get('criteria') or 'нужна проверка глазами'}"

    # --- новые типы ---

    async def _check_file_exists(self, check, cwd, task, project):
        """Файл на месте. Путь строго внутри каталога проекта.

        Без песочницы критерий `../../etc/passwd` проходил бы всегда,
        а `C:/Windows/win.ini` превращал бы приёмку в фикцию.
        """
        root = Path(cwd).resolve()
        raw = str(check.get("path") or "")
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False, (f"file_exists: путь {raw!r} выходит за пределы каталога "
                           f"проекта — такой критерий не проверяется")
        if candidate.exists():
            return True, ""
        near = ", ".join(sorted(p.name for p in candidate.parent.iterdir())[:8]) \
            if candidate.parent.exists() else "каталога нет"
        return False, f"file_exists: нет {raw}. Рядом: {near}"

    async def _check_dom(self, check, cwd, task, project):
        """Селектор на живой странице. Через headless-браузер, а не разбор HTML:
        половина верстки сегодня дорисовывается скриптами, и по сырому
        HTML такой проверки не сделать."""
        url = str(check.get("url") or "")
        selector = str(check.get("selector") or "")
        need = int(check.get("min_count", 1) or 1)
        timeout = float(check.get("timeout", DEFAULT_TIMEOUT_SEC) or DEFAULT_TIMEOUT_SEC)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return False, ("dom: playwright не установлен — проверка невыполнима "
                           "(pip install playwright && playwright install chromium)")

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    page = await browser.new_page()
                    page.set_default_timeout(timeout * 1000)
                    await page.goto(url, wait_until="networkidle",
                                    timeout=timeout * 1000)
                    count = await page.locator(selector).count()
                    if count >= need:
                        return True, ""
                    title = (await page.title())[:80]
                    body = (await page.inner_text("body"))[:200].replace("\n", " ")
                    return False, (f"dom: {url} — селектор {selector!r} найден {count} раз, "
                                   f"нужно {need}. Заголовок: {title!r}. "
                                   f"Начало страницы: {body!r}")
                finally:
                    await browser.close()
        except Exception as e:
            return False, f"dom: {url} не проверить — {type(e).__name__}: {e}"

    async def _check_screenshot(self, check, cwd, task, project):
        """Снимки на трёх ширинах и вердикт модели по описанию критерия.

        Это assisted-проверка: модель видит картинку и говорит, похоже ли на
        описанное. Приговором такой вердикт не считается — но он ловит
        разъехавшуюся вёрстку, которую селектором не поймать.
        """
        url = str(check.get("url") or "")
        criteria = str(check.get("criteria") or "")
        timeout = float(check.get("timeout", DEFAULT_TIMEOUT_SEC) or DEFAULT_TIMEOUT_SEC)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return False, "screenshot: playwright не установлен — проверка невыполнима"
        if self.client is None:
            return False, ("screenshot: судить снимок нечем — "
                           + missing_secret_message("ANTHROPIC_API_KEY").splitlines()[0])

        shots: list[tuple[str, bytes]] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    for width, height in VIEWPORTS:
                        page = await browser.new_page(viewport={"width": width,
                                                                "height": height})
                        page.set_default_timeout(timeout * 1000)
                        await page.goto(url, wait_until="networkidle",
                                        timeout=timeout * 1000)
                        shots.append((f"{width}px", await page.screenshot(full_page=False)))
                        await page.close()
                finally:
                    await browser.close()
        except Exception as e:
            return False, f"screenshot: {url} не снять — {type(e).__name__}: {e}"

        content = [{"type": "text",
                    "text": (f"Критерий: {criteria}\n\nНиже снимки одной страницы на трёх "
                             f"ширинах. Ответь СТРОГО JSON: "
                             f'{{"verdict":"PASS|FAIL","reason":"коротко"}}')}]
        for label, png in shots:
            content.append({"type": "text", "text": f"Ширина {label}:"})
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": "image/png",
                                       "data": base64.b64encode(png).decode()}})

        t0 = time.monotonic()
        resp = await self.client.messages.create(
            model=cfg.judge_model, max_tokens=800,
            messages=[{"role": "user", "content": content}])
        await self._charge(task, project, resp, time.monotonic() - t0)
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(raw)
        except Exception:
            return False, f"screenshot: судья вернул не-JSON: {raw[:200]!r}"
        if str(data.get("verdict")).upper() == "PASS":
            return True, ""
        return False, f"screenshot: {data.get('reason') or 'не соответствует описанию'}"

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
            return False, "судья недоступен: " + missing_secret_message("ANTHROPIC_API_KEY")

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
            # молча засчитывать PASS нельзя: непрочитанный вердикт — это не приёмка
            log.warning("судья вернул не-JSON для задачи %s: %r", task.id, raw[:300])
            return False, "судья не ответил: вердикт не разобран"
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
