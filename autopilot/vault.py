"""Хранилище секретов клиента.

Правила, которые здесь держатся жёстко:

* значения лежат ТОЛЬКО в зашифрованном файле (Fernet, ключ из VAULT_KEY);
* в БД и в Google-таблице хранится ссылка `{{SECRET:NAME}}`, но не значение;
* в промпт для Claude Code значение не подставляется никогда — вместо ссылки
  туда уходит `$NAME`, а само значение приходит агенту через окружение процесса;
* всё, что известно хранилищу, вымарывается из логов фильтром `SecretMasker`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import cfg

log = logging.getLogger("vault")

SECRET_RE = re.compile(r"\{\{SECRET:([A-Za-z0-9_.-]+)\}\}")
MASK = "***"

# Значения короче этого не маскируем: замена всех «1» в логе на *** делает
# логи нечитаемыми и всё равно ничего не защищает. Короткий секрет — плохой секрет.
MIN_MASKABLE_LEN = 4


def ref(name: str) -> str:
    return "{{SECRET:%s}}" % name


def refs_in(text: str) -> list[str]:
    """Имена секретов, упомянутых в тексте."""
    return list(dict.fromkeys(SECRET_RE.findall(text or "")))


def to_env_names(text: str) -> str:
    """{{SECRET:FTP_PASS}} -> $FTP_PASS. Именно это видит агент в промпте."""
    return SECRET_RE.sub(lambda m: f"${m.group(1)}", text or "")


class Vault:
    def __init__(self, path: str | Path | None = None, key: str | bytes | None = None):
        self.path = Path(path or cfg.vault_path)
        raw_key = key if key is not None else cfg.vault_key
        if isinstance(raw_key, str):
            raw_key = raw_key.strip().encode()
        self._fernet = Fernet(raw_key) if raw_key else None
        self._cache: dict[str, str] | None = None
        self._cache_stamp: float | None = None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    # ---------- диск ----------

    def _read(self) -> dict[str, str]:
        if not self.enabled:
            return {}
        if not self.path.exists():
            return {}
        stamp = self.path.stat().st_mtime_ns
        if self._cache is not None and self._cache_stamp == stamp:
            return self._cache
        try:
            data = json.loads(self._fernet.decrypt(self.path.read_bytes()).decode())
        except InvalidToken:
            raise RuntimeError(
                f"{self.path} не расшифровывается текущим VAULT_KEY.\n"
                f"  • сменил VAULT_KEY, а файл остался старым — верни прежний ключ;\n"
                f"  • файл достался от другой установки (например, от чужого "
                f"прогона) — удали {self.path} и заведи секреты заново:\n"
                f"      python scripts/vault_cli.py add ANTHROPIC_API_KEY\n"
                f"  • VAULT_PATH указывает не на тот файл — проверь .env") from None
        except Exception as e:
            raise RuntimeError(f"хранилище {self.path} повреждено: {e}") from None
        self._cache = {str(k): str(v) for k, v in data.items()}
        self._cache_stamp = stamp
        return self._cache

    def _write(self, data: dict[str, str]) -> None:
        if not self.enabled:
            raise RuntimeError("VAULT_KEY не задан — секреты хранить негде")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._fernet.encrypt(json.dumps(data, ensure_ascii=False).encode())
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(self.path)          # атомарно: половины файла не бывает
        try:
            self.path.chmod(0o600)      # на Windows почти no-op, на *nix — по делу
        except OSError:
            pass
        self._cache = dict(data)
        self._cache_stamp = self.path.stat().st_mtime_ns

    # ---------- API ----------

    def set(self, name: str, value: str) -> str:
        data = dict(self._read())
        data[name] = value
        self._write(data)
        return ref(name)

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def names(self) -> list[str]:
        return sorted(self._read())

    def delete(self, name: str) -> bool:
        data = dict(self._read())
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    def values(self) -> list[str]:
        """Только для маскирования логов. Наружу не отдавать."""
        return [v for v in self._read().values() if v]

    def env_for(self, *texts: str) -> dict[str, str]:
        """Значения, которые надо положить в окружение процесса-исполнителя."""
        env: dict[str, str] = {}
        missing: list[str] = []
        for text in texts:
            for name in refs_in(text):
                val = self.get(name)
                if val is None:
                    missing.append(name)
                else:
                    env[name] = val
        if missing:
            log.warning("в хранилище нет секретов: %s", ", ".join(sorted(set(missing))))
        return env

    def mask(self, text: str) -> str:
        if not text:
            return text
        for value in self.values():
            if len(value) >= MIN_MASKABLE_LEN and value in text:
                text = text.replace(value, MASK)
        return text

    def mask_bytes(self, blob: bytes) -> bytes:
        if not blob:
            return blob
        return self.mask(blob.decode(errors="replace")).encode()


vault = Vault()


class SecretMasker(logging.Filter):
    """Вымарывает значения секретов из всего, что уходит в лог.

    Ставится на root-логгер, поэтому накрывает и наши сообщения, и чужие,
    и вывод Claude Code, который executor логирует как есть.
    """

    def __init__(self, source: Vault | None = None):
        super().__init__()
        self.source = source or vault

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.source.enabled:
            return True
        try:
            text = record.getMessage()
        except Exception:
            return True
        masked = self.source.mask(text)
        if masked != text:
            record.msg = masked
            record.args = ()
        return True


def install_log_masking(source: Vault | None = None) -> SecretMasker | None:
    """Вешает маскирование на root-логгер и на все его обработчики.

    Фильтр логгера не применяется к записям, пришедшим от дочерних логгеров,
    поэтому дублируем его на обработчиках — там проходит вообще всё.
    """
    src = source or vault
    if not src.enabled:
        log.info("VAULT_KEY не задан — маскировать нечего")
        return None
    masker = SecretMasker(src)
    root = logging.getLogger()
    root.addFilter(masker)
    for handler in root.handlers:
        handler.addFilter(masker)
    return masker


# ---------- единая точка получения секретов ----------

# Значения-заглушки из .env.example. Без этой проверки `sk-ant-...` считается
# настоящим ключом, код уходит в API и получает 401 вместо внятного «ключа нет».
PLACEHOLDERS = ("sk-ant-...", "sk-...", "1abc...", "changeme", "todo", "xxx")

ENV_FILE = "окружение"
ENV_DOTFILE = ".env"
SOURCE_VAULT = "vault"


def _usable(value: str | None) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    if v in PLACEHOLDERS or v.endswith("..."):
        return False
    return True


def _dotenv_values() -> dict[str, str]:
    from dotenv import dotenv_values
    try:
        return dotenv_values(cfg.root / ".env") or {}
    except Exception:
        return {}


def resolve_secret(name: str, source: Vault | None = None) -> tuple[str | None, str]:
    """Ищет значение в трёх местах по порядку: vault → окружение → .env.

    Возвращает (значение, откуда). Vault первый, потому что это единственное
    место, где значение лежит зашифрованным; окружение и .env — запасные
    варианты для тех, кто хранилище ещё не завёл.
    """
    v = source or vault
    if v.enabled:
        try:
            found = v.get(name)
        except RuntimeError:
            found = None          # хранилище не читается — скажем об этом отдельно
        if _usable(found):
            return found, SOURCE_VAULT

    dotfile = _dotenv_values().get(name)
    env = os.environ.get(name)

    # dotenv на старте уже влил .env в окружение, поэтому «окружение» засчитываем
    # только если значение там ОТЛИЧАЕТСЯ от файла — иначе источник это .env
    if _usable(env) and env != dotfile:
        return env, ENV_FILE
    if _usable(dotfile):
        return dotfile, ENV_DOTFILE
    if _usable(env):
        return env, ENV_FILE
    return None, ""


def missing_secret_message(name: str) -> str:
    return (
        f"{name} не найден ни в одном из трёх мест:\n"
        f"  1. хранилище secrets.enc — python scripts/vault_cli.py add {name}\n"
        f"  2. переменная окружения — {name}=... python -m autopilot.main\n"
        f"  3. файл .env рядом с проектом — строка {name}=...\n"
        f"Значения-заглушки вроде «sk-ant-...» настоящим ключом не считаются."
    )


def anthropic_key(source: Vault | None = None) -> str | None:
    return resolve_secret("ANTHROPIC_API_KEY", source)[0]


def anthropic_key_source(source: Vault | None = None) -> str:
    return resolve_secret("ANTHROPIC_API_KEY", source)[1]
