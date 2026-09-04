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
    "secrets.enc", "sub/secrets.enc", "config/id_rsa", "keys/id_ed25519",
    ".claude/settings.json", "accounts.toml", "autopilot.db",
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
    """На Windows `Secrets.ENC` и `secrets.enc` — один файл, и запрет один."""
    assert policy.denied("Secrets.ENC") and policy.denied("SUB\\Accounts.TOML")


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
    write(tmp_path, ".claude/settings.json", "{}")     # настройки инструмента
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


# ---------- правки после состязательной проверки ----------

@pytest.mark.parametrize("path", [
    # ВСЁ ЭТО АГЕНТ СОЗДАЁТ ПО ДЕЛУ, а первая версия денилиста на каждом
    # клала в корень PAUSED, который снимает только рука. `SYSTEM_RULES`
    # прямо велит агенту читать доступы из окружения — то есть подразумевает
    # `.env` в проекте заказчика.
    ".env", ".env.example", "backend/.env.local",
    "client_app/android/app/upload.keystore",
    "backend/certs/localhost.pem",
])
def test_files_of_the_clients_app_are_not_denied(path):
    assert not policy.denied(path)


@pytest.mark.parametrize("path", [
    # А чужое — ловится: хранилище установки и приватные ключи
    "secrets.enc", "accounts.toml", "autopilot.db",
    ".claude/settings.json", "config/id_rsa",
])
def test_someone_elses_files_are_still_denied(path):
    assert policy.denied(path)


def test_short_vault_values_are_not_treated_as_leaks(tmp_path):
    """Перехватчик на входе НАМЕРЕННО жадный и кладёт в хранилище логины
    тоже. Голое `value in text` находило `postgres` в честной строке
    подключения и останавливало ВСЮ работу файлом PAUSED."""
    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    v.set("DB_USER", "postgres")
    assert policy.leaked("DATABASE_URL = 'postgresql://postgres@localhost/app'", v) == []


def test_long_values_are_still_caught(tmp_path):
    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    v.set("API_KEY", "sk-live-9f2c7a41b8e6d503")
    assert policy.leaked("KEY = 'sk-live-9f2c7a41b8e6d503'", v) == ["API_KEY"]


def test_truncated_snapshot_yields_no_phantom_changes(tmp_path):
    """Обрезанный по MAX_FILES обход останавливается на разных файлах,
    и всё, что попало в первый слепок и не попало во второй, выглядело
    УДАЛЁННЫМ. Дальше это читают как факт — решение по выдумке."""
    before = worktrace.Snapshot({"a.py": (1, 1), "b.py": (1, 1)}, ok=False,
                                why="больше 40000 файлов")
    after = worktrace.Snapshot({"a.py": (1, 1)}, ok=True)
    changed = worktrace.diff(before, after)
    assert changed.deleted == () and changed.paths == ()
    assert changed.ok is False
