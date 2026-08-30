"""Приложение проекта поднимается на время приёмки — и гасится после.

Критерий при этом не меняется ни на символ. Меняется то, что по адресу
теперь отвечает именно код проекта, а не что попало: раньше `http
localhost:8000` мог случайно пройти на чужом сервере или упасть на пустом
порту, и ни то ни другое ничего не говорило о работе агента.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
from pathlib import Path

import pytest

from autopilot import appserver
from autopilot.db import Task


def task_with(checks: list[dict]) -> Task:
    return Task(id=1, project_id=1, title="t", acceptance=checks)


def test_port_is_taken_from_http_and_dom_criteria():
    assert appserver.needed_port(task_with(
        [{"type": "http", "url": "http://localhost:8000/admin/"}])) == "8000"
    assert appserver.needed_port(task_with(
        [{"type": "dom", "url": "http://127.0.0.1:9001/", "selector": "b"}])) == "9001"


def test_no_port_when_nothing_needs_localhost():
    assert appserver.needed_port(task_with(
        [{"type": "shell", "cmd": "pytest"}])) is None
    assert appserver.needed_port(task_with(
        [{"type": "http", "url": "https://example.com/"}])) is None


async def test_nothing_started_when_criteria_do_not_need_it(tmp_path):
    async with appserver.running_for(
            task_with([{"type": "shell", "cmd": "pytest"}]), tmp_path) as started:
        assert started is False


async def test_someone_elses_server_is_not_touched(tmp_path):
    """Порт занят — значит его поднял владелец или соседняя задача.
    Глушить чужой процесс мы не вправе ни при каких обстоятельствах."""
    class Quiet(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        backend = tmp_path / "backend" / "app"
        backend.mkdir(parents=True)
        task = task_with([{"type": "http", "url": f"http://localhost:{port}/"}])
        async with appserver.running_for(task, tmp_path) as started:
            assert started is False, "мы не должны были поднимать свой сервер"
            assert appserver.port_busy(port), "чужой сервер должен остаться живым"
        assert appserver.port_busy(port), "чужой сервер погасили — так нельзя"
    finally:
        srv.shutdown()


async def test_missing_backend_is_not_an_error(tmp_path):
    """Нет `backend/app` или `.venv` — поднимать нечего. Приёмка увидит
    закрытый порт ровно как раньше, а молча засчитать критерий нельзя."""
    task = task_with([{"type": "http", "url": "http://localhost:8123/"}])
    async with appserver.running_for(task, tmp_path) as started:
        assert started is False


def test_port_busy_is_honest_about_a_free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert appserver.port_busy(free) is False
