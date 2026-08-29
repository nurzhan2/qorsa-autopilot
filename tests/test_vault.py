"""Секреты: в БД только ссылки, в промпте только имена, в логах только звёздочки."""
from __future__ import annotations

import io
import json
import logging

from conftest import make_access, make_project
from cryptography.fernet import Fernet
from sqlalchemy import select

from autopilot.db import AccessItem, Project, Session, Task
from autopilot.executor import build_prompt
from autopilot.vault import SecretMasker, Vault, ref, refs_in

SECRET = "sUp3r-Secret-Ftp-Pass-9f2b"


def make_vault(tmp_path, key=None) -> Vault:
    return Vault(path=tmp_path / "secrets.enc", key=key or Fernet.generate_key())


def test_vault_roundtrip(tmp_path):
    """Записал, прочитал, значение совпало; на диске в открытом виде его нет."""
    key = Fernet.generate_key()
    v = make_vault(tmp_path, key)
    assert v.enabled

    assert v.set("FTP_PASS", SECRET) == "{{SECRET:FTP_PASS}}"
    assert v.get("FTP_PASS") == SECRET
    assert v.names() == ["FTP_PASS"]

    blob = (tmp_path / "secrets.enc").read_bytes()
    assert SECRET.encode() not in blob, "секрет лежит на диске открытым текстом"
    assert b"FTP_PASS" not in blob, "даже имя не должно торчать наружу"

    # новый экземпляр с тем же ключом читает то же самое — значит дело в файле,
    # а не в оперативной памяти процесса
    reopened = make_vault(tmp_path, key)
    assert reopened.get("FTP_PASS") == SECRET

    assert v.delete("FTP_PASS") is True
    assert v.get("FTP_PASS") is None
    assert v.delete("FTP_PASS") is False


def test_vault_wrong_key_is_loud(tmp_path):
    v = make_vault(tmp_path)
    v.set("A", SECRET)
    other = Vault(path=tmp_path / "secrets.enc", key=Fernet.generate_key())
    try:
        other.names()
    except RuntimeError as e:
        assert "VAULT_KEY" in str(e)
    else:
        raise AssertionError("чужим ключом хранилище прочиталось")


async def test_secret_never_in_prompt(db, tmp_path, monkeypatch):
    """build_prompt() для задачи с секретом не содержит значения ни в каком виде."""
    v = make_vault(tmp_path)
    v.set("FTP_PASS", SECRET)
    monkeypatch.setattr("autopilot.executor.vault", v)

    p = await make_project(title="сайт")
    async with Session() as s:
        proj = await s.get(Project, p.id)
        proj.brief = {"ftp": ref("FTP_PASS"), "host": "example.kz"}
        await s.commit()
        proj = await s.get(Project, p.id)

    task = Task(id=1, project_id=p.id, title="выложить на хостинг",
                prompt=f"залей сборку по FTP, пароль {ref('FTP_PASS')}",
                acceptance=[{"type": "shell", "cmd": f"curl -u u:{ref('FTP_PASS')} ..."}])

    prompt = build_prompt(task, proj)

    assert SECRET not in prompt
    assert json.dumps(SECRET) not in prompt
    assert "{{SECRET:" not in prompt, "ссылка должна превратиться в имя переменной"
    assert "$FTP_PASS" in prompt, "агент должен узнать, из какой переменной брать"


async def test_secret_ref_stored_not_value(db, tmp_path, monkeypatch):
    """В БД у пункта доступа лежит ссылка, а не значение."""
    v = make_vault(tmp_path)
    v.set("SSH_KEY", SECRET)
    p = await make_project()
    item = await make_access(p.id, "SSH", "ssh", "verified")
    async with Session() as s:
        stored = await s.get(AccessItem, item.id)
        stored.secret_ref = ref("SSH_KEY")
        await s.commit()

    async with Session() as s:
        rows = (await s.execute(select(AccessItem.secret_ref))).scalars().all()
    assert rows == ["{{SECRET:SSH_KEY}}"]
    assert SECRET not in str(rows)
    assert refs_in(rows[0]) == ["SSH_KEY"]


def test_log_masking(tmp_path, caplog):
    """Значение секрета, попавшее в лог, замаскировано."""
    v = make_vault(tmp_path)
    v.set("FTP_PASS", SECRET)

    logger = logging.getLogger("test.masking")
    masker = SecretMasker(v)
    logger.addFilter(masker)
    try:
        with caplog.at_level(logging.INFO, logger="test.masking"):
            logger.info("агент написал: пароль %s, спасибо", SECRET)
            logger.info("и в другой строке тоже %s", SECRET)
    finally:
        logger.removeFilter(masker)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in text, "секрет утёк в лог"
    assert "***" in text
    assert "агент написал" in text, "маскирование не должно съедать остальное сообщение"


def test_log_masking_covers_the_traceback(tmp_path):
    """Секрет из ТЕКСТА ИСКЛЮЧЕНИЯ тоже не должен попасть в лог.

    Поймано на первом живом запуске автопилота, и это был настоящий утечка,
    а не гипотеза: httpx поднял HTTPStatusError с полным URL запроса, и токен
    бота лёг в logs/ открытым. В соседней строке того же лога он был честно
    замаскирован — дыра была ровно одна: `getMessage()` фильтруется, а
    исключение форматтер приклеивает потом, из `record.exc_info`, мимо фильтра.
    """
    v = make_vault(tmp_path)
    token = "8617379268:AAFwDBQJtYd2l3YMw7ScScuzim1HH41ASg8"
    v.set("BOT_TOKEN", token)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.masking.traceback")
    logger.handlers = [handler]
    logger.propagate = False
    masker = SecretMasker(v)
    logger.addFilter(masker)
    handler.addFilter(masker)
    try:
        try:
            raise RuntimeError(
                f"Client error '400' for url "
                f"'https://api.telegram.org/bot{token}/sendMessage'")
        except RuntimeError:
            logger.exception("не смог уведомить владельца")
    finally:
        logger.handlers = []
        logger.removeFilter(masker)

    text = stream.getvalue()
    assert token not in text, "токен утёк в лог через трейсбэк"
    assert "***" in text
    assert "RuntimeError" in text, "маскирование не должно съедать сам трейсбэк"


def test_mask_bytes_covers_subprocess_output(tmp_path):
    """Вывод Claude Code пишется на диск — там секрета тоже быть не должно."""
    v = make_vault(tmp_path)
    v.set("FTP_PASS", SECRET)
    raw = json.dumps({"result": f"подключился с паролем {SECRET}"}).encode()
    assert SECRET.encode() not in v.mask_bytes(raw)


def test_short_values_are_not_masked(tmp_path):
    """Короткое значение не маскируем: логи превратились бы в решето из звёзд."""
    v = make_vault(tmp_path)
    v.set("PIN", "12")
    assert v.mask("версия 12 собрана") == "версия 12 собрана"


def test_vault_disabled_without_key(tmp_path):
    v = Vault(path=tmp_path / "secrets.enc", key="")
    assert v.enabled is False
    assert v.names() == []
    assert v.mask("что угодно") == "что угодно"


# ---------- заглушки .env не должны утекать в окружение дочернего процесса ----------

def test_is_placeholder_recognises_stub_values():
    """Живой случай: задача 523 получила ANTHROPIC_API_KEY=sk-ant-... и упала
    с «401 API key is invalid» — CLI пытался авторизоваться заглушкой из
    .env.example, хотя настоящий ключ лежит в vault и подписке не нужен вовсе.
    """
    from autopilot.config import _is_placeholder

    for stub in ("sk-ant-...", "sk-...", "1abc...", "changeme", "todo", "xxx",
                "что-угодно-с-многоточием..."):
        assert _is_placeholder(stub) is True, f"{stub!r} не распознан как заглушка"

    for real in ("sk-ant-api03-realkeylookinglonghex1234567890",
                "postgresql+asyncpg://postgres:pass@localhost:5432/db", "12"):
        assert _is_placeholder(real) is False, f"{real!r} ошибочно принят за заглушку"


def test_env_placeholder_does_not_leak_into_process(tmp_path):
    """Заглушка из .env не должна оказаться в os.environ вообще.

    load_dotenv() кладёт .env в окружение ПРОЦЕССА как есть, а окружение
    дочернего claude собирается из os.environ напрямую (Executor._secret_env,
    CliBackend.ask) — заглушка утекала бы в любой такой subprocess. Проверяем
    ту же логику, что реально чистит os.environ при импорте autopilot.config,
    на синтетическом примере, а не полагаемся на содержимое настоящего .env.
    """
    import os

    from autopilot.config import _is_placeholder

    os.environ["_TEST_STUB_SECRET"] = "sk-ant-..."
    try:
        for name, val in list(os.environ.items()):
            if _is_placeholder(val):
                del os.environ[name]
        assert "_TEST_STUB_SECRET" not in os.environ
    finally:
        os.environ.pop("_TEST_STUB_SECRET", None)
