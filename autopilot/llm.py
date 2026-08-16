"""Откуда берутся ответы модели: платный API или подписка через CLI.

С этого момента бриф, план и судья ходят к модели не напрямую, а через один
интерфейс с двумя реализациями:

* `api` — `anthropic` SDK. Списывает реальные деньги, считается в суточном
  бюджете, ограничен только балансом;
* `cli` — `claude -p`. Денег не списывает, ест квоту подписки в пятичасовом
  окне, ограничен этим окном и ничем больше.

Выбор задаётся отдельно для каждого потребителя (`LLM_BACKEND_BRIEF`,
`LLM_BACKEND_PLAN`, `LLM_BACKEND_JUDGE`), потому что они разные по цене и
по важности: судью можно держать на подписке, а бриф вернуть на API,
когда там снова появятся деньги.

**CLI здесь — это чистый вызов модели, а не агент.** Инструменты выключены
все до одного: агенту дай Read и Bash, и он пойдёт изучать репозиторий,
сжигая ходы и время на работу, которой у него не просили. Нам нужен один
ответ на один промпт.

**`--resume` не передаётся никогда.** Судья обязан смотреть на диф свежими
глазами: продолжив сессию исполнителя, он будет оценивать собственную работу
и найдёт её прекрасной. Это не оптимизация контекста, это подмена приёмки.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import json
import logging
import os
import re
import shutil
import tempfile
import time

from . import guard
from .config import cfg
from .vault import anthropic_key, missing_secret_message

log = logging.getLogger("llm")

API, CLI = "api", "cli"

# Признаки того, что мы упёрлись в квоту подписки, а не сломали запрос.
# Отличать обязательно: упор — это «подожди», ошибка — это «почини».
LIMIT_MARKERS = (
    "usage limit", "rate limit", "quota", "limit reached", "limit exceeded",
    "too many requests", "resets at", "try again later", "overloaded",
    "лимит", "превышен", "исчерпан",
)


class LLMError(RuntimeError):
    """Модель не ответила по технической причине: не найдена, не авторизована."""


class LimitReached(RuntimeError):
    """Упор в квоту. НЕ ошибка задачи и НЕ повод засчитывать попытку.

    Отдельный тип нужен, чтобы вызывающий код не спутал его с провалом
    работы: четыре упора подряд увели бы живую задачу в эскалацию, хотя
    с ней всё в порядке и нужно просто подождать.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dc.dataclass(frozen=True)
class Reply:
    """Ответ модели и то, во что он обошёлся.

    `cost_usd` от CLI — ОЦЕНКА, а не списание: подписка деньгами не считается.
    Поэтому счётчики раздельные, и суточный бюджет смотрит только на `billed`.
    """

    text: str
    stop_reason: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    backend: str = API
    seconds: float = 0.0
    # Какая модель РЕАЛЬНО отвечала. Не то же самое, что мы попросили:
    # CLI по умолчанию берёт Opus, а его недельная квота на порядок меньше
    # и нужна владельцу. Проверять надо по факту, а не по нашему намерению.
    models: tuple[str, ...] = ()
    # Токены создания кэша. У CLI их десятки тысяч на каждый вызов —
    # это его системный промпт, и он съедает квоту независимо от нашего
    cache_creation_tokens: int = 0

    @property
    def model_names(self) -> str:
        return ", ".join(self.models) if self.models else "?"

    @property
    def billed(self) -> bool:
        """Списаны ли за это реальные деньги."""
        return self.backend == API


def backend_for(consumer: str) -> str:
    """Какой бэкенд у этого потребителя: brief | plan | judge."""
    value = str(getattr(cfg, f"llm_backend_{consumer}", "") or cfg.llm_backend).strip().lower()
    return value if value in (API, CLI) else CLI


def looks_like_limit(text: str, returncode: int | None = None) -> bool:
    """Упор в квоту или обычная ошибка.

    Смотрим и на текст, и на код возврата: CLI сообщает об исчерпании
    по-разному в разных версиях, и держаться за одну формулировку — способ
    однажды принять упор за поломку и увести задачу в эскалацию.
    """
    body = str(text or "").lower()
    if any(m in body for m in LIMIT_MARKERS):
        return True
    # 429 приходит и кодом возврата тоже
    return returncode == 429


def retry_after_from(text: str) -> float | None:
    """Когда сбрасывается окно, если CLI это сказал."""
    m = re.search(r"resets?\s+(?:at|in)\s+([^\n.]+)", str(text or ""), re.IGNORECASE)
    if not m:
        return None
    tail = m.group(1)
    hours = re.search(r"(\d+)\s*h", tail, re.IGNORECASE)
    mins = re.search(r"(\d+)\s*m", tail, re.IGNORECASE)
    if hours or mins:
        return (int(hours.group(1)) * 3600 if hours else 0) + \
               (int(mins.group(1)) * 60 if mins else 0)
    return None


# ---------- реализации ----------

class ApiBackend:
    """Прямые вызовы Anthropic API. Стоят денег и считаются в бюджете."""

    name = API

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            key = anthropic_key()
            if not key:
                raise LLMError(missing_secret_message("ANTHROPIC_API_KEY"))
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=key)
        return self._client

    async def ask(self, prompt: str, *, system: str = "", model: str = "",
                  max_tokens: int = 8000, content=None) -> Reply:
        from .verifier import PRICE_IN, PRICE_OUT

        body = content if content is not None else prompt
        t0 = time.monotonic()
        try:
            resp = await self.client.messages.create(
                model=model or cfg.judge_model, max_tokens=max_tokens,
                **({"system": system} if system else {}),
                messages=[{"role": "user", "content": body}])
        except Exception as e:
            if looks_like_limit(str(e)):
                raise LimitReached(f"API: {e}") from e
            raise
        seconds = time.monotonic() - t0

        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "input_tokens", 0) if usage else 0
        tout = getattr(usage, "output_tokens", 0) if usage else 0
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        return Reply(text=text, stop_reason=str(getattr(resp, "stop_reason", "") or ""),
                     cost_usd=tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT,
                     input_tokens=tin, output_tokens=tout, backend=API, seconds=seconds)


def resolve_cli(binary: str) -> tuple[str | None, list[str]]:
    """Путь к CLI и, если без него никак, обёртка-интерпретатор.

    npm на Windows ставит `claude.CMD` — обёртку, которую CreateProcess
    запустить не может, а cmd.exe запускает, но режет командную строку
    на 8191 символе и портит кириллицу. Рядом при этом лежит настоящий
    `claude.exe`; берём его и обходимся без интерпретатора вовсе.
    """
    found = shutil.which(binary)
    if not found:
        return None, []
    if not found.lower().endswith((".cmd", ".bat")):
        return found, []

    root = os.path.dirname(found)
    for candidate in (
        os.path.join(root, "node_modules", "@anthropic-ai", "claude-code",
                     "bin", "claude.exe"),
        os.path.join(root, "claude.exe"),
    ):
        if os.path.exists(candidate):
            log.debug("вместо обёртки %s беру %s", found, candidate)
            return candidate, []
    # настоящего бинаря не нашли — придётся через интерпретатор
    return found, [os.environ.get("COMSPEC", "cmd.exe"), "/c"]


class CliBackend:
    """`claude -p` — подписка вместо денег.

    Инструменты выключены полностью, каталог временный, сессия всегда новая.
    """

    name = CLI

    def __init__(self, binary: str | None = None):
        self.binary = binary or cfg.claude_bin

    async def ask(self, prompt: str, *, system: str = "", model: str = "",
                  max_tokens: int = 8000, content=None) -> Reply:
        if content is not None:
            # Картинки CLI в таком виде не принимает. Молча слать текст без
            # изображения нельзя: судья по скриншоту вынес бы вердикт, ничего
            # не увидев, и это была бы приёмка на пустом месте
            raise LLMError("CLI-бэкенд не умеет картинки — для screenshot-проверок "
                           "нужен LLM_BACKEND_JUDGE=api")
        resolved, launcher = resolve_cli(self.binary)
        if not resolved:
            raise LLMError(
                f"не нашёл {self.binary!r} в PATH. Claude Code CLI не установлен "
                f"или не в PATH; поставь его либо переключись на API "
                f"(LLM_BACKEND_*=api)")

        # Промпт уходит В STDIN, а не аргументом.
        #
        # На Windows npm ставит claude как claude.CMD, запуск идёт через
        # cmd.exe, а у него командная строка ограничена 8191 символом. Промпт
        # брифа — пятнадцать тысяч, и вызов падал с обрубленной абракадаброй
        # вместо внятной ошибки: cmd ещё и калечил кириллицу. Через stdin
        # длина не ограничена ничем и кодировка своя.
        args = [
            *launcher, resolved, "-p",
            "--output-format", "json",
            # Ни одного инструмента: это вызов модели, а не агент. С Read
            # и Bash модель уходит изучать репозиторий и жжёт ходы впустую.
            #
            # Пустой allowlist, и НИКАКОГО --disallowedTools: замерено на живом
            # CLI, что перечисление инструментов по именам подтягивает их
            # описания в контекст и учетверяет расход кэша (26k против 6.7k
            # токенов на вызов). Запрещать поимённо то, что и так не разрешено,
            # — платить за список запретов.
            "--allowedTools", "",
            "--max-turns", "1",
            # sonnet намеренно и ВСЕГДА явно: по умолчанию CLI берёт Opus,
            # чья недельная квота на порядок меньше и нужна владельцу
            "--model", model or cfg.cli_model,
            # ЗАМЕНА системного промпта, а не дописывание к нему, плюс отказ
            # от динамических секций. Замерено на живом CLI: с дефолтным
            # промптом каждый вызов создаёт ~40 тысяч токенов кэша, с этими
            # двумя флагами — НОЛЬ. Это двадцатикратная разница в расходе
            # пятичасового окна, то есть разница между «работает весь день»
            # и «встало после трёх вызовов». Нам нужен чистый вызов модели,
            # инструкции Claude Code про инструменты и репозиторий здесь
            # только мешают.
            "--system-prompt", system or "Ты отвечаешь строго по инструкции ниже.",
            "--exclude-dynamic-system-prompt-sections",
        ]
        # --resume НЕ передаётся никогда, см. docstring модуля

        # Каталог временный: в репозитории проекта CLI подхватил бы его
        # CLAUDE.md и настройки, а нам нужен чистый вызов
        workdir = tempfile.mkdtemp(prefix="qorsa-llm-")
        t0 = time.monotonic()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            # Дочерний claude не должен пережить родителя ни при каких
            # обстоятельствах: осиротевший процесс продолжает жечь ту же
            # квоту пятичасового окна, отвечать некому, и заметить это
            # можно только по опустевшему окну
            guard.watch_child(proc)
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=cfg.cli_timeout_sec)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise LLMError(f"{self.binary} не ответил за {cfg.cli_timeout_sec}s")
        finally:
            # сюда попадает и отмена по сигналу: процесс убиваем сами,
            # ждать его в этот момент уже нечем
            if proc is not None:
                if getattr(proc, "returncode", 0) is None:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
                guard.forget_child(proc)
            shutil.rmtree(workdir, ignore_errors=True)
        seconds = time.monotonic() - t0

        stdout = (out or b"").decode(errors="replace")
        stderr = (err or b"").decode(errors="replace")

        # РАЗБИРАЕМ РАНЬШЕ, ЧЕМ ИЩЕМ УПОР В КВОТУ, и это не косметика.
        #
        # Раньше `looks_like_limit` шёл первым и обыскивал весь stdout —
        # то есть ОТВЕТ САМОЙ МОДЕЛИ. План приложения доставки, честно
        # расписавший «rate limit» для API, был объявлен упором в квоту:
        # `is_error:false`, `stop_reason:end_turn`, сто тысяч токенов
        # выхода — и всё выброшено, а вместе с этим по правилам limits.py
        # встала вся работа и владельцу ушло уведомление. Двадцать шесть
        # минут работы модели в мусор из-за слова в тексте.
        #
        # Успешный конверт упором не бывает по определению: про исчерпание
        # квоты нам говорит оболочка, а не содержимое ответа.
        data = None
        if stdout.strip():
            try:
                data = json.loads(stdout)
            except ValueError:
                data = None
        if isinstance(data, dict) and not data.get("is_error") and proc.returncode == 0:
            pass                      # успех, вниз к разбору полей
        else:
            body = ""
            if isinstance(data, dict):
                body = str(data.get("result") or "")
            body = body or stderr or stdout
            if looks_like_limit(body, proc.returncode):
                raise LimitReached(f"квота подписки исчерпана: {body[:300]}",
                                   retry_after_from(body))
            if data is None:
                raise LLMError(f"{self.binary} отдал не-JSON: {stdout[:300]!r}")
            raise LLMError(f"{self.binary} вернул {proc.returncode}: {body[:300]}")

        usage = data.get("usage") or {}
        # Какая модель отвечала НА САМОМ ДЕЛЕ. CLI по умолчанию берёт Opus,
        # и одна опечатка в имени модели молча увела бы работу на квоту,
        # которая нужна владельцу. Проверяем по факту
        models = tuple(sorted((data.get("modelUsage") or {}).keys()))
        if any("opus" in m.lower() for m in models):
            log.warning("CLI ответил моделью %s — просили %s. Квота Opus нужна "
                        "владельцу, проверь --model", ", ".join(models),
                        model or cfg.cli_model)

        return Reply(
            text=str(data.get("result") or ""),
            stop_reason=str(data.get("stop_reason") or ""),
            # ОЦЕНКА, не списание: подписка деньгами не считается
            cost_usd=float(data.get("total_cost_usd") or 0.0),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            models=models, backend=CLI, seconds=seconds)


def make(consumer: str, client=None):
    """Бэкенд для потребителя: brief | plan | judge."""
    kind = backend_for(consumer)
    if kind == API:
        return ApiBackend(client=client)
    return CliBackend()
