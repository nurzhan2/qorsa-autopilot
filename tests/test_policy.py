"""Политика: секрет, записанный в файл, и путь, которого работа не касается.

Зеркало `assert_no_secrets` на ВЫХОДЕ. На входе такой рубеж был с фазы 2,
на выходе не было вовсе — при том что `autonomy.PROMPT_NOTE` прямо
предупреждает: получив заглушку вместо ключа, агент начинает её обходить
и хардкодить «временный».
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from autopilot import policy, worktrace
from autopilot.vault import Vault


@pytest.fixture
def store(tmp_path):
    v = Vault(path=tmp_path / "secrets.enc", key=Fernet.generate_key())
    v.set("FTP_PASS", "s3cret-пароль-клиента")
    return v


def write(root, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------- денилист ----------

@pytest.mark.parametrize("path", [
    ".env", ".env.local", "sub/.env", "backend/.env.production",
    "secrets.enc", "config/id_rsa", "keys/server.pem",
    ".claude/settings.json", "accounts.toml",
])
def test_denied_paths(path):
    assert policy.denied(path)


@pytest.mark.parametrize("path", [
    # ЭТО РАБОТА АГЕНТА, а не нарушение. Денилист прототипа запрещает
    # `migrations/`, `auth/`, `payments/` — он написан для цикла, который
    # правит чужой репозиторий. Наш агент пишет приложение заказчика с нуля,
    # и всё это — буквально пункты ТЗ.
    "backend/alembic/versions/001_init.py",
    "backend/app/auth/login.py",
    "backend/app/payments/yookassa.py",
    "client_app/lib/main.dart",
    "backend/tests/test_cart.py",
])
def test_work_is_not_denied(path):
    assert not policy.denied(path)


def test_denied_is_case_insensitive():
    """На Windows `.ENV` и `.env` — один файл, и запрет обязан быть один."""
    assert policy.denied(".ENV") and policy.denied("SUB\\.Env")


# ---------- утечка ----------

def test_leak_reports_name_not_value(store):
    names = policy.leaked("DB_PASSWORD = 's3cret-пароль-клиента'", store)
    assert names == ["FTP_PASS"]


def test_leak_says_nothing_without_a_match(store):
    assert policy.leaked("DB_PASSWORD = os.environ['FTP_PASS']", store) == []


def test_review_finds_a_secret_written_to_a_file(tmp_path, store):
    before = worktrace.snapshot(tmp_path)
    write(tmp_path, "backend/app/config.py",
          "FTP_PASS = 's3cret-пароль-клиента'  # временно")
    changed = worktrace.diff(before, worktrace.snapshot(tmp_path))

    verdict = policy.review(changed, tmp_path, store)
    assert not verdict.clean and verdict.halting
    assert verdict.violations[0].kind == "leak"
    # В ОТЧЁТЕ ЗНАЧЕНИЯ НЕТ: он уходит в лог и на страницу
    text = "\n".join(verdict.report())
    assert "FTP_PASS" in text and "s3cret" not in text


def test_review_flags_a_denied_path(tmp_path, store):
    before = worktrace.snapshot(tmp_path)
    write(tmp_path, ".env", "TOKEN=123")
    changed = worktrace.diff(before, worktrace.snapshot(tmp_path))

    verdict = policy.review(changed, tmp_path, store)
    assert {v.kind for v in verdict.violations} == {"deny_path"}


def test_review_is_quiet_on_honest_work(tmp_path, store):
    before = worktrace.snapshot(tmp_path)
    write(tmp_path, "backend/app/config.py",
          "FTP_PASS = os.environ['FTP_PASS']")
    write(tmp_path, "backend/alembic/versions/001.py", "def upgrade(): ...")
    changed = worktrace.diff(before, worktrace.snapshot(tmp_path))

    verdict = policy.review(changed, tmp_path, store)
    assert verdict.clean and verdict.checked


def test_incomplete_snapshot_is_not_a_clean_verdict(tmp_path, store):
    """«Нарушений не найдено» и «не смотрели» — разные ответы, и второй
    обязан быть виден."""
    changed = worktrace.Changed(ok=False, why="слепок «до» не снят")
    verdict = policy.review(changed, tmp_path, store)
    assert verdict.clean and verdict.checked is False
    assert "не проверено" in verdict.summary()


def test_disabled_vault_finds_nothing(tmp_path):
    """Без VAULT_KEY искать нечего — и падать тоже не за что."""
    assert policy.leaked("что угодно", Vault(path=tmp_path / "x.enc", key=None)) == []
