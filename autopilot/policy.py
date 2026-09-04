"""Чего агенту делать нельзя — проверяется по ФАКТУ, а не по обещанию.

Вход в модель проект уже стережёт: `assert_no_secrets()` не даёт отправить
промпт, в котором оказалось значение из хранилища (`brief.py:355`, вызовы
в `brief` и `planner`). **Выхода такой проверки не было вовсе** — при том,
что `autonomy.PROMPT_NOTE` прямо предупреждает: получив заглушку вместо
ключа, агент начинает её обходить и хардкодить «временный» ключ. То есть
известная склонность агента есть, а рубежа против неё нет.

Здесь два правила, и оба ДЕТЕРМИНИРОВАННЫЕ:

1. **утечка секрета в файл** — значение из vault оказалось в файле, который
   агент написал. Это зеркало `assert_no_secrets`, только на выходе;
2. **запретный путь** — агент тронул то, что к работе над заказом отношения
   не имеет: чужие ключи, настройки самой установки, её хранилище секретов.

Чего в денилисте НЕТ и не будет, хотя есть у прототипа (loop-engineering,
`gate.yaml`): `**/migrations/**`, `**/auth/**`, `**/payments/**`, `**/billing/**`.
Тот денилист написан для цикла, который ПРАВИТ ЧУЖОЙ репозиторий, а наш агент
ПИШЕТ ПРИЛОЖЕНИЕ ЗАКАЗЧИКА С НУЛЯ: alembic-миграции, модуль авторизации и
интеграция платёжного шлюза — это его прямая работа, буквально пункты ТЗ.
Денилист, запрещающий делать работу, снимут целиком — вместе с полезным.

Порога `maxFiles: 10` тоже нет: задача «Инициализация backend-монолита»
законно создаёт десятки файлов, а `flutter create` — сотню.

**Остановка идёт через файл `PAUSED`, а не через `limits.state`.** Соблазн
переиспользовать готовый тормоз был, и он неверен: пауза по квоте снимается
сама через `LIMIT_BACKOFF_START_SEC` (пять минут), а дашборд объясняет её
словами «упёрлись в квоту подписки, само отпустит». Инцидент безопасности,
который рассасывается через пять минут и объяснён неправдой, — это не
остановка. `PAUSED` снимает только рука.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

from .vault import MIN_MASKABLE_LEN, vault as default_vault

log = logging.getLogger("policy")

# Пути ВНУТРИ рабочего каталога, которых работа над заказом не касается
# никогда. Список нарочно короткий: каждый пункт должен быть таким, чтобы
# его нарушение нельзя было объяснить выполнением задачи.
DENY = (
    ".env", ".env.*", "**/.env", "**/.env.*",
    "**/.ssh/**", "**/id_rsa*", "**/id_ed25519*", "**/*.pem", "**/*.p12",
    "**/*.kdbx", "**/*.keystore", "**/*.jks",
    "secrets.enc", "**/secrets.enc",
    ".claude/**", "**/.claude/**",          # настройки инструмента, не работа
    "accounts.toml", "**/accounts.toml",
    "autopilot.db", "**/autopilot.db*",     # база самой установки
)

KINDS = ("leak", "deny_path", "outside")


@dataclass(frozen=True)
class Violation:
    kind: str
    path: str
    what: str


@dataclass(frozen=True)
class Review:
    """Вердикт политики по одной сессии агента."""

    violations: tuple[Violation, ...] = ()
    # слепок был неполон — «нарушений не найдено» читать нельзя
    checked: bool = True
    why: str = ""

    @property
    def clean(self) -> bool:
        return not self.violations

    @property
    def halting(self) -> bool:
        """Есть ли то, из-за чего работу надо остановить целиком.

        Утечка секрета — да: значение уже лежит в файле, и следующая задача
        разнесёт его дальше. Запретный путь — да по той же причине: агент
        полез туда, куда не должен был, и почему — неизвестно.
        """
        return bool(self.violations)

    def summary(self) -> str:
        if self.clean:
            return "нарушений нет" if self.checked else f"не проверено: {self.why}"
        by_kind: dict[str, int] = {}
        for v in self.violations:
            by_kind[v.kind] = by_kind.get(v.kind, 0) + 1
        names = {"leak": "секрет в файле", "deny_path": "запретный путь",
                 "outside": "файл вне каталога заказа"}
        return "; ".join(f"{names.get(k, k)}: {n}" for k, n in by_kind.items())

    def report(self) -> list[str]:
        """Строки для владельца и для журнала. ЗНАЧЕНИЙ СЕКРЕТОВ ЗДЕСЬ НЕТ —
        только имя секрета и путь: отчёт уходит в лог и на страницу."""
        return [f"{v.what}: {v.path}" for v in self.violations]


def denied(path: str) -> bool:
    """Попадает ли путь под денилист. Сравнение по posix-виду и без регистра:
    на Windows `.ENV` и `.env` — один файл."""
    p = str(path).replace("\\", "/").lower()
    return any(fnmatch.fnmatch(p, pattern.lower()) for pattern in DENY)


def leaked(text: str, vault=None) -> list[str]:
    """Имена секретов, значения которых нашлись в тексте.

    Возвращает ИМЕНА, а не значения: результат уходит в лог и владельцу.
    Порог длины тот же, что у маскирования, — короткое значение совпадает
    случайно (`root`, `1234`), и такой сигнал был бы шумом.
    """
    v = vault or default_vault
    if not v.enabled or not text:
        return []
    out = []
    for name, value in v.items():
        if value and len(value) >= MIN_MASKABLE_LEN and value in text:
            out.append(name)
    return out


def review(changed, root, vault=None, max_bytes: int = 2_000_000) -> Review:
    """Проверить то, что агент реально изменил за сессию.

    `changed` — `worktrace.Changed`. Нечитаемый файл считается ПРОВЕРЕННЫМ
    не был: об этом говорит `checked=False`, а не пустой список нарушений.
    """
    from pathlib import Path

    base = Path(root)
    problems: list[str] = []
    violations: list[Violation] = []

    for rel in changed.paths:
        if denied(rel):
            violations.append(Violation("deny_path", rel,
                                        "тронут путь, запрещённый политикой"))

    # Утечку ищем только в том, что создано или изменено: удалённый файл
    # прочитать нечем, и это не потеря — секрет из него уже не утечёт.
    for rel in sorted({*changed.created, *changed.modified}):
        path = base / rel
        try:
            if path.stat().st_size > max_bytes:
                continue                     # большой бинарник не читаем
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError) as e:
            problems.append(f"{rel}: {e}")
            continue
        for name in leaked(text, vault):
            violations.append(Violation("leak", rel,
                                        f"значение секрета {{{{SECRET:{name}}}}} "
                                        f"записано в файл"))

    checked = changed.ok and not problems
    why = changed.why or (f"не прочитано {len(problems)}: {problems[0][:160]}"
                          if problems else "")
    return Review(tuple(violations), checked=checked, why=why)
