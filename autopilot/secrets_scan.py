"""Перехват секретов во входящих сообщениях.

Клиенты присылают пароли прямо в чат — это данность, а не досадное исключение.
Поэтому детектор работает ДО записи сообщения в БД: найденное значение уезжает
в vault, а в тексте остаётся `{{SECRET:NAME}}`.

Порог намеренно низкий: спрятать лишнее дешевле, чем сохранить чужой пароль
открытым текстом. Если сомнение есть — прячем.

Клиенту про перехват не сообщаем: подтверждаем получение доступа и всё.
"""
from __future__ import annotations

import logging
import re

from .vault import MASK, Vault, ref
from .vault import vault as default_vault

log = logging.getLogger("scan")

# Значения короче этого не считаем секретом: «ок», «да», «1234» — это шум,
# а не пароль, и прятать их значит превратить переписку в решето.
MIN_SECRET_LEN = 6
# После явной метки («пароль X», «логин X») порог ниже: там значение названо
# прямым текстом, и промах дороже лишнего срабатывания. Побочный эффект —
# логины тоже уезжают в vault. Это осознанно.
LABELLED_MIN_LEN = 4

# Слова, после которых идёт значение. Ловим и «пароль: X», и «пароль X».
LABELS = r"(?:парол[ья]?|пасс(?:ворд)?|password|pwd|логин|login|user(?:name)?|ключ|key|токен|token|секрет|secret)"

# Заведомо не секреты, даже если стоят после метки
STOPWORDS = {
    "не", "нет", "да", "будет", "будут", "пришлю", "пришлём", "потом", "later",
    "такой", "тот", "же", "выше", "ниже", "тут", "здесь", "прежний", "старый",
    "новый", "смотри", "см", "в", "на", "от", "для", "и", "а", "или",
}

PATTERNS: list[tuple[str, re.Pattern, int]] = [
    # ssh-ключ целиком: группа 0 — весь блок
    ("ssh_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL), 0),
    # схема://логин:ПАРОЛЬ@хост — прячем только пароль, хост и логин полезны.
    # Группа жадная: в пароле сплошь и рядом есть собственная @
    ("url_cred", re.compile(
        r"(?:[a-z][a-z0-9+.-]{1,15})://[^\s:/@]+:(\S{3,})@[^\s@]+", re.IGNORECASE), 1),
    # голое логин:ПАРОЛЬ@хост без схемы
    ("bare_cred", re.compile(
        r"(?<![\w:/])[\w.\-]{2,}:(\S{3,})@[\w.\-]+\.[a-z]{2,}", re.IGNORECASE), 1),
    # ключи известных сервисов
    ("api_key", re.compile(
        r"\b(?:sk-[A-Za-z0-9_\-]{16,}"
        r"|gh[pousr]_[A-Za-z0-9]{16,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|AIza[0-9A-Za-z_\-]{20,}"
        r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
        r"|glpat-[A-Za-z0-9_\-]{16,}"
        r"|AKIA[0-9A-Z]{16})\b"), 0),
    # «пароль: Qw3rty!», «пароль от панели Adm1nPanel», «логин админки admin».
    # До двух русских слов-связок между меткой и значением: без этого
    # захватывался предлог, а сам логин оставался в открытом виде
    ("labelled", re.compile(
        LABELS + r"\s*(?:—|-|:|=|это)?\s+(?:[а-яё]+\s+){0,2}?([^\s,;]{"
        + str(LABELLED_MIN_LEN) + r",})", re.IGNORECASE), 1),
]


# Пароль из одних русских букв не бывает: это слово, а не секрет.
# Без этой проверки «логин админки» прятало слово «админки».
_CYRILLIC_WORD = re.compile(r"^[а-яё]+$", re.IGNORECASE)

# Хвостовая пунктуация предложения. «!» и «?» НЕ трогаем — они сплошь и рядом
# часть самого пароля, а откусив их, мы сохраним значение, которое не подойдёт.
TRAILING_PUNCT = ".,;:»«\"'()"


def _looks_like_value(v: str, minimum: int = MIN_SECRET_LEN) -> bool:
    if len(v) < minimum:
        return False
    if v.lower() in STOPWORDS:
        return False
    if _CYRILLIC_WORD.match(v):
        return False
    if v.startswith("{{SECRET:"):
        return False        # уже спрятано
    return True


# Токен, похожий на пароль: буквы вперемешку с цифрами или спецсимволами.
# Чисто словарные «панели», «хостинга» и домены вроде example.kz сюда не попадают.
_TOKEN_RE = re.compile(r"[^\s,;]{6,}")
_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA = re.compile(r"[A-Za-z]")
_HAS_SPECIAL = re.compile(r"[!@#$%^&*_+=?~]")
_LABEL_RE = re.compile(LABELS, re.IGNORECASE)


def _labelled_line_hits(text: str) -> list[tuple[str, str]]:
    """Строка содержит слово-метку — ищем в ней всё, что похоже на пароль.

    Соседний токен ловит основной шаблон, но живые сообщения выглядят как
    «пароль от панели Adm1nPanel»: между меткой и значением стоят два слова.
    Разбирать русскую грамматику регуляркой бессмысленно, поэтому смотрим
    на форму самого значения.
    """
    hits: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if not _LABEL_RE.search(line):
            continue
        for token in _TOKEN_RE.findall(line):
            token = token.strip(TRAILING_PUNCT)
            if len(token) < MIN_SECRET_LEN or _LABEL_RE.fullmatch(token):
                continue
            mixed = _HAS_DIGIT.search(token) and _HAS_ALPHA.search(token)
            if not (mixed or _HAS_SPECIAL.search(token)):
                continue
            if token.startswith("{{SECRET:"):
                continue
            hits.append(("labelled_line", token))
    return hits


def find_secrets(text: str) -> list[tuple[str, str]]:
    """[(вид, значение), ...] в порядке появления. Значения не логировать."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, rx, group in PATTERNS:
        for m in rx.finditer(text or ""):
            value = m.group(group)
            if not value:
                continue
            value = value.strip()
            # у url_cred/api_key/ssh_key границы заданы синтаксисом — там резать
            # нечего; у текстовых меток снимаем только пунктуацию предложения
            if kind in ("labelled", "labelled_line"):
                value = value.strip(TRAILING_PUNCT)
            minimum = LABELLED_MIN_LEN if kind == "labelled" else MIN_SECRET_LEN
            if kind != "ssh_key" and not _looks_like_value(value, minimum):
                continue
            if value in seen:
                continue
            seen.add(value)
            found.append((kind, value))

    for kind, value in _labelled_line_hits(text):
        if value not in seen:
            seen.add(value)
            found.append((kind, value))

    # Отсеиваем вложенные совпадения: на `ftp://ivan:pass@host` шаблон без схемы
    # захватывает «//ivan:pass» поверх уже найденного «pass». Оставляем короткое —
    # оно и есть собственно секрет, длинное только мусорит в хранилище.
    result = []
    for kind, value in found:
        if any(other != value and other in value for _, other in found):
            continue
        result.append((kind, value))
    return result


def scrub(text: str, *, project_id: int | None = None, chat_id: str = "",
          vault: Vault | None = None, store: bool = True) -> tuple[str, list[str]]:
    """Возвращает (текст с плейсхолдерами, имена созданных секретов).

    Без VAULT_KEY значение сохранить негде — тогда оно просто вырезается.
    Потерять пароль лучше, чем записать его в базу открытым.

    `store=False` — вырезать, но НЕ класть в хранилище. Нужно для пробных
    прогонов импорта: раньше `--dry-run` честно ничего не писал в базу, но
    секреты в vault складывал, и один и тот же пароль оседал там дважды под
    разными именами. Пробный прогон обязан не менять состояние вообще.
    """
    v = vault or default_vault
    hits = find_secrets(text or "")
    if not hits:
        return text, []

    names: list[str] = []
    out = text
    for kind, value in hits:
        if not store:
            # пробный прогон: показать, что нашли бы, и ничего не менять
            out = out.replace(value, MASK)
            log.info("нашёл секрет (%s) — пробный прогон, в хранилище не кладу", kind)
            names.append(kind)
            continue
        if not v.enabled:
            out = out.replace(value, MASK)
            log.warning("нашёл секрет (%s), но VAULT_KEY не задан — вырезал без сохранения", kind)
            continue
        name = _unique_name(v, kind, project_id, chat_id)
        v.set(name, value)
        out = out.replace(value, ref(name))
        names.append(name)
        log.info("перехватил секрет из переписки: %s -> %s", kind, ref(name))
    return out, names


def _unique_name(v: Vault, kind: str, project_id: int | None, chat_id: str) -> str:
    base = f"P{project_id}" if project_id else f"CHAT{re.sub(r'[^A-Za-z0-9]', '', chat_id)[:12] or 'X'}"
    base = f"{base}_{kind.upper()}"
    existing = set(v.names())
    i = 1
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"
