"""Провалы, которые случались НЕ по вине агента, и больше не тратят попытку.

Все три воспроизводят живые случаи проекта 8. Общее у них одно: приёмка
объявляла дефектом работы то, что работой не являлось вовсе.
"""
from __future__ import annotations

import http.server
import threading

import pytest

from autopilot import manual
from autopilot.executor import _looks_like_auth
from autopilot.verifier import Verifier


# ---------- 1. критерий expect:3xx был недостижим по построению ----------

@pytest.fixture
def redirecting_server():
    """Сервер, который ЧЕСТНО отдаёт 302 на /admin/ — правильное поведение,
    которого и требовала задача 531."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/admin/login"):
                body = b"<html>login</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "/admin/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


async def test_redirect_criterion_can_actually_pass(redirecting_server):
    """Живой провал 531 целиком: сервер отдавал 302, приёмка писала
    «-> 200, ожидалось 302».

    Клиент открывался с follow_redirects=True и сравнивал статус КОНЕЧНОЙ
    страницы — то есть критерий не мог пройти НИКОГДА, как бы правильно
    агент ни работал. Четыре сессии, эскалация и заблокированные соседние
    задачи из-за одного флага.
    """
    ok, msg = await Verifier()._http(f"{redirecting_server}/admin/", 302)
    assert ok is True, f"критерий expect:302 всё ещё недостижим: {msg}"


async def test_redirect_criterion_still_fails_when_there_is_no_redirect(
        redirecting_server):
    """Проверка не смягчена: страница без редиректа критерий не проходит."""
    ok, msg = await Verifier()._http(f"{redirecting_server}/admin/login", 302)
    assert ok is False
    assert "ожидалось 302" in msg


async def test_2xx_criteria_still_follow_redirects(redirecting_server):
    """Для 2xx поведение прежнее: редирект по дороге к странице нормален."""
    ok, _ = await Verifier()._http(f"{redirecting_server}/admin/", 200)
    assert ok is True


async def test_defect_names_where_the_redirect_leads(redirecting_server):
    """Ждали 301, сервер отдал 302 — честный провал, и человеку сразу видно,
    куда редирект ведёт. Разбирать такое придётся глазами, и Location
    экономит этот заход."""
    ok, msg = await Verifier()._http(f"{redirecting_server}/admin/", 301)
    assert ok is False
    assert "ведёт на" in msg and "/admin/login" in msg


# ---------- 2. недостающая обвязка приёмки — не дефект работы ----------

@pytest.mark.parametrize("defect", [
    "dom: playwright не установлен — проверка невыполнима",
    "судья недоступен: нет ANTHROPIC_API_KEY",
    "screenshot: судить снимок нечем — нужен LLM_BACKEND_JUDGE=api",
    "file_exists: путь '../../etc/passwd' выходит за пределы каталога проекта",
])
def test_missing_verification_plumbing_goes_to_human_not_to_retry(defect):
    """Браузер не поставится от правки кода агента, ключ судьи не появится,
    а путь за пределами каталога не станет проверяемым — сколько ни повторяй.

    Все эти строки верификатор формировал и раньше, но ни один маркер их
    не ловил: задача честно писала «проверка невыполнима» и всё равно шла
    на повтор, четыре раза подряд.
    """
    assert manual.needs_human([defect]) is True


def test_a_real_defect_is_still_retried():
    """Настоящий дефект работы по-прежнему идёт на повтор, а не к человеку."""
    assert manual.needs_human(["`pytest` exit=1\n2 failed"]) is False


# ---------- 3. упор в квоту и истёкшая сессия ----------

@pytest.mark.parametrize("text", [
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "not logged in, please run /login",
    "authentication_error: invalid api key",
])
def test_auth_failure_is_recognised(text):
    assert _looks_like_auth(text) is True


def test_ordinary_failure_is_not_taken_for_auth():
    """Слова из отчёта модели не должны превращаться в глобальный стоп."""
    assert _looks_like_auth("агент упёрся в потолок ходов: сделано 80") is False
    assert _looks_like_auth("`pytest` exit=1") is False
