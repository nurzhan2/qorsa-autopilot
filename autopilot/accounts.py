"""Компании владельца. Их несколько, и это не одно и то же, что проекты.

Qorsa Studio и Hustle Design — два юрлица одного человека. У каждой свой бот,
свои клиенты, свои чаты и своя таблица заказов. Общего у них ровно одно:
процесс, который их обслуживает.

**Почему TOML, а не .env.** Требование было — добавление третьей компании
без правок кода. В .env для этого пришлось бы либо заводить переменные с
префиксом (`QORSA_OWNER_TG_ID`, `HUSTLE_OWNER_TG_ID`) и где-то держать
список кодов, либо парсить строку с разделителями. Первое — правка кода при
каждой новой компании, второе — самодельный формат. В TOML компания
описывается блоком `[[account]]`, и добавление третьей это дописанный блок.
`tomllib` лежит в стандартной библиотеке с 3.11, новой зависимости нет.

**Токенов здесь нет.** В `bot_token_ref` лежит ИМЯ секрета в vault, а не сам
токен: `accounts.toml` — обычный конфиг, его читают глазами и правят руками,
и класть туда ключ от бота значит вернуть ровно ту проблему, ради которой
заведён vault.
"""
from __future__ import annotations

import dataclasses as dc
import logging
import os
import tomllib
from pathlib import Path

from .config import cfg

log = logging.getLogger("accounts")

CONFIG_NAME = "accounts.toml"

# Код компании, в которую уезжают проекты, заведённые до мультиаккаунтности.
# Он же — единственная компания в режиме совместимости с .env
DEFAULT_CODE = "qorsa"


@dc.dataclass(frozen=True)
class Account:
    """Одна компания. Неизменяемая: конфиг перечитывается целиком."""

    code: str                      # qorsa | hustle | ... — стабильный ключ
    name: str                      # как компания называется для людей
    owner_tg_id: str = ""          # владелец В ЧАТАХ ЭТОЙ КОМПАНИИ
    owner_max_id: str = ""
    bot_tg_id: str = ""            # id самого бота: его реплики не идут в ТЗ
    bot_max_id: str = ""
    bot_token_ref: str = ""        # ИМЯ секрета в vault, не значение
    max_token_ref: str = ""
    handle: str = ""               # @qorsastudio — для отчётов и опознания
    sheet_id: str = ""
    sheet_tab: str = "Заказы"
    signature: str = ""            # как бот подписывается клиенту
    active: bool = True

    def __post_init__(self) -> None:
        if not str(self.code).strip():
            raise ValueError("у компании должен быть code")
        if not str(self.name).strip():
            raise ValueError(f"у компании {self.code!r} должно быть name")

    @property
    def owner_ids(self) -> dict[str, str]:
        """{transport: owner_id} — пусто там, где мессенджер не настроен."""
        return {k: v for k, v in (("telegram", str(self.owner_tg_id).strip()),
                                  ("max", str(self.owner_max_id).strip())) if v}

    @property
    def bot_ids(self) -> dict[str, str]:
        return {k: v for k, v in (("telegram", str(self.bot_tg_id).strip()),
                                  ("max", str(self.bot_max_id).strip())) if v}

    def owner_configured(self) -> bool:
        """Без id владельца система не отличит его реплики от клиентских."""
        return bool(self.owner_ids)


def config_path() -> Path:
    return Path(os.getenv("ACCOUNTS_FILE") or (cfg.root / CONFIG_NAME))


def _from_env() -> list[Account]:
    """Режим совместимости: одна компания, собранная из .env.

    Нужен не для красоты. До мультиаккаунтности вся настройка жила в .env, и
    установка без `accounts.toml` обязана продолжать работать — иначе апдейт
    кода молча ломает рабочую систему. Тесты и демо тоже идут по этой ветке.
    """
    return [Account(
        code=DEFAULT_CODE,
        name=os.getenv("ACCOUNT_NAME", "Qorsa Studio"),
        owner_tg_id=str(cfg.owner_tg_id or ""),
        owner_max_id=str(cfg.owner_max_id or ""),
        bot_tg_id=str(cfg.bot_tg_id or ""),
        bot_max_id=str(cfg.bot_max_id or ""),
        bot_token_ref="TG_BOT_TOKEN",
        max_token_ref="MAX_TOKEN",
        sheet_id=str(cfg.sheet_id or ""),
        sheet_tab=str(cfg.sheet_tab or "Заказы"),
        signature=os.getenv("ACCOUNT_SIGNATURE", ""),
        active=True,
    )]


def _one(raw: dict) -> Account:
    known = {f.name for f in dc.fields(Account)}
    unknown = set(raw) - known
    if unknown:
        # молча глотать опечатку нельзя: «sheet_di» осталось бы незамеченным,
        # а компания синкалась бы не в ту таблицу
        raise ValueError(f"компания {raw.get('code')!r}: непонятные поля {sorted(unknown)}")
    data = {k: raw[k] for k in raw if k in known}
    data.setdefault("signature", str(data.get("name", "")))
    return Account(**data)


def load(path: Path | None = None) -> list[Account]:
    """Читает accounts.toml. Нет файла — одна компания из .env."""
    path = path or config_path()
    if not path.exists():
        log.info("%s нет — работаю одной компанией из .env", path.name)
        return _from_env()

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    raw = data.get("account") or []
    if isinstance(raw, dict):                 # [account.qorsa] вместо [[account]]
        raw = [{**v, "code": v.get("code", k)} for k, v in raw.items()]
    accounts = [_one(r) for r in raw]

    codes = [a.code for a in accounts]
    dupes = {c for c in codes if codes.count(c) > 1}
    if dupes:
        # два блока с одним кодом — это молча потерянная компания
        raise ValueError(f"{path.name}: код компании повторяется: {sorted(dupes)}")
    if not accounts:
        raise ValueError(f"{path.name}: не описано ни одной компании")
    return accounts


def active(path: Path | None = None) -> list[Account]:
    return [a for a in load(path) if a.active]


def by_code(code: str, path: Path | None = None) -> Account | None:
    for a in load(path):
        if a.code == str(code):
            return a
    return None


def shared_sheets(accounts: list[Account]) -> dict[str, list[str]]:
    """{sheet_id: [коды компаний]} для таблиц, которые делят несколько компаний.

    По умолчанию таблицы раздельные. Но общая одна на всех — законный режим,
    и синк обязан о нём знать: в такой таблице проекты разных компаний лежат
    вперемешку, и различать их можно только по колонке «Компания».
    """
    out: dict[str, list[str]] = {}
    for a in accounts:
        key = str(a.sheet_id or "").strip()
        if key:
            out.setdefault(key, []).append(a.code)
    return {k: v for k, v in out.items() if len(v) > 1}
