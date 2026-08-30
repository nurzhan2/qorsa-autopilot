"""Поднять приложение проекта на время приёмки — и погасить после.

Зачем. Критерии `http` и `dom` бьют в `localhost:8000`, а поднимать там
что-либо никто не обязан. Задача 531 («Веб-админка») из-за этого четырежды
провалила приёмку: критерий ждал `302`, получал `200` — то есть отвечал
не тот код, который агент только что написал, а всё, что случайно оказалось
на порту. Порт при этом бывает и вовсе пуст, и тогда красными становятся
разом все четырнадцать localhost-критериев плана.

Почему это ПОЧИНКА ОКРУЖЕНИЯ, а не смягчение приёмки. Критерий не меняется
ни на символ: как было `http localhost:8000/admin/ -> 302`, так и осталось.
Меняется только то, что теперь по этому адресу отвечает ИМЕННО код проекта,
а не что попало. Проверка от этого становится строже, а не мягче: раньше она
могла случайно пройти на чужом сервере.

Логика не новая — она была написана и проверена в `scripts/live_run.py`,
но жила в СКРИПТЕ, то есть в ручном стенде. Полоса `verify` главного цикла
её не видела вовсе. Это ровно тот разрыв, который в этом проекте уже
случался между `llm.py` и `executor.py`: урок выучен в одном месте и
не перенесён в другое, потому что второе ни разу не гоняли живьём.

Границы честные:
* точка входа берётся стандартная для зафиксированного стека —
  `app.main:app` через uvicorn в `backend/`. Это не догадка о чужом проекте:
  стек выбрал владелец, а `.venv` создал сам агент;
* **чужой сервер не трогаем.** Если порт уже занят, значит его поднял
  владелец или соседняя задача — глушить его мы не вправе;
* не удалось поднять — приёмка увидит закрытый порт ровно как раньше.
  Молча считать критерий пройденным нельзя ни при каких обстоятельствах.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from pathlib import Path

log = logging.getLogger("appserver")

# Сколько ждём, пока приложение встанет. TCP-порт не слушается, пока uvicorn
# не закончил импорт, а импорт занимает секунды. Задача 499 падала не по своей
# вине именно в этом окне: проверка стучалась один раз сразу.
READY_TIMEOUT = 30.0
POLL_EVERY = 0.5

_PORT_RE = re.compile(r"(?:localhost|127\.0\.0\.1):(\d+)")


def needed_port(task) -> str | None:
    """Порт, в который стучатся критерии задачи. None — сервер не нужен."""
    for check in (getattr(task, "acceptance", None) or []):
        if not isinstance(check, dict):
            continue
        if check.get("type") not in ("http", "dom"):
            continue
        m = _PORT_RE.search(str(check.get("url") or ""))
        if m:
            return m.group(1)
    return None


def port_busy(port: str | int) -> bool:
    """Слушает ли кто-нибудь этот порт прямо сейчас."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def _venv_python(backend: Path) -> Path | None:
    for sub, name in (("Scripts", "python.exe"), ("bin", "python")):
        candidate = backend / ".venv" / sub / name
        if candidate.exists():
            return candidate
    return None


@contextlib.asynccontextmanager
async def running_for(task, workspace: Path):
    """Контекст: приложение проекта поднято, пока идут проверки задачи."""
    port = needed_port(task)
    backend = Path(workspace) / "backend"
    venv_py = _venv_python(backend) if port else None

    if not port or venv_py is None or not (backend / "app").exists():
        yield False
        return

    if port_busy(port):
        # Чужой сервер не трогаем: его мог поднять владелец руками или
        # соседняя задача, идущая параллельно. Глушить его мы не вправе
        log.info("порт %s уже занят — поднимать не буду, проверяю как есть", port)
        yield False
        return

    log.info("поднимаю приложение проекта для приёмки: uvicorn app.main:app :%s", port)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(venv_py), "-m", "uvicorn", "app.main:app",
            "--port", str(port), "--host", "127.0.0.1", cwd=str(backend),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        log.warning("сервер не запустился (%s) — проверяю как есть", e)
        yield False
        return

    try:
        ready = await _wait_ready(proc, port)
        if not ready:
            log.warning("приложение не ответило за %.0fс — критерии проверю "
                        "всё равно, но порт скорее всего закрыт", READY_TIMEOUT)
        yield ready
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _wait_ready(proc, port: str) -> bool:
    """Настоящий readiness-поллинг, а не один стук сразу.

    Отказ соединения в первую секунду — это «ещё не встал», а не «не
    работает». Задача 499 падала не по своей вине именно на этом.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + READY_TIMEOUT
    while loop.time() < deadline:
        if proc.returncode is not None:
            tail = b""
            with contextlib.suppress(Exception):
                tail = await proc.stderr.read()
            from .textenc import decode_console
            log.warning("сервер упал при старте: %s",
                        decode_console(tail)[-400:])
            return False
        if port_busy(port):
            log.info("приложение отвечает на :%s", port)
            return True
        await asyncio.sleep(POLL_EVERY)
    return False
