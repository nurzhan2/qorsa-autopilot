"""Режим самостоятельности: работать с заглушками, ключи подставить в конце.

Зачем. Полоса `build` не выдаёт слот, пока весь чеклист доступов не `verified`
(гейт 2 из трёх). На живом проекте 8 это остановило всё: автономность плана
71% задач, а идти в работу не может ни одна — половина ждёт ключа платёжного
шлюза, который клиент выдаст неизвестно когда. При этом «Управление статусами
заказа» и «Схема БД» к тому ключу не относятся никак.

Что делает режим:

* недостающим пунктам чеклиста кладётся **заглушка** в vault и ставится
  статус `placeholder` — гейт такие пункты не считает блокирующими;
* агент получает заглушку в окружении, как получил бы настоящий ключ, и пишет
  код, который читает переменную окружения, а не хардкодит значение;
* когда владелец вставляет настоящее значение, заглушка заменяется, пункт
  становится `verified`, а задачи, ждавшие этот ключ, идут на переприёмку.

Чего режим НЕ делает, и это главное:

**он не разрешает приёмке врать.** Проверка, которой нужен настоящий ключ,
НЕ ВЫПОЛНЯЕТСЯ вовсе и записывается как отложенная. Не «выполняется и как-то
трактуется» — именно не выполняется: гонять живой запрос к платёжному шлюзу
с фальшивым ключом бессмысленно, а трактовать его ответ пришлось бы гаданием.
Задача с хоть одной отложенной проверкой не закрывается никогда — она уходит
в `awaiting_keys` и ждёт ключа. Попытка при этом НЕ засчитывается: работа тут
ни при чём, ровно как при упоре в квоту (см. limits.py).

Граница честности, которую надо помнить: отложенность распознаётся по двум
ДЕТЕРМИНИРОВАННЫМ признакам — критерий ссылается на секрет-заглушку, либо
в выводе проверки встретился маркер заглушки. Критерий, которому настоящий
ключ нужен неявно (`pytest tests/test_payments.py`, внутри которого живой
вызов шлюза), ни одним из них не ловится и провалится как обычная задача.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select

from .db import AccessItem, Project, Session, Task, utcnow
from .vault import refs_in
from .vault import vault as default_vault

log = logging.getLogger("autonomy")

# Значение-заглушка узнаётся по префиксу. Оно намеренно уродливое и
# самоописывающееся: если такое утечёт в лог, в отчёт агента или в ответ
# внешнего сервиса, по строке сразу видно, что это не настоящий ключ.
PLACEHOLDER_PREFIX = "PLACEHOLDER::"

# Статус пункта чеклиста в этом режиме. Отдельный от `needed` намеренно:
# `needed` значит «ждём и стоим», `placeholder` — «ждём, но работаем».
PLACEHOLDER = "placeholder"

# Статус задачи: сделана, но проверить нечем, пока нет настоящего ключа.
# Не `needs_human`: там ждут РЕШЕНИЯ владельца, а здесь ждут ЗНАЧЕНИЯ
# от клиента, и решать владельцу нечего.
AWAITING_KEYS = "awaiting_keys"

# Имя переменной окружения из названия пункта чеклиста.
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")

# Транслитерация для имён: «Платёжный шлюз» -> PLATEZHNYY_SHLYUZ.
# Тот же приём, что в projectdir.slug, и по той же причине: имя переменной
# окружения обязано быть латинским.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Читаемый хвост обрезается, поэтому имя ЗАКАНЧИВАЕТСЯ номером пункта.
# Без него два пункта проекта 8 — «Платёжная система (карты + СБП в рублях)»
# и «Платёжная система (карты + СБП для рублёвых платежей русскими картами)» —
# давали ОДНО имя P8_API_KEY_PLATEZHNAYA_SISTEMA_KARTY_SB: вставленное
# значение одного молча затирало бы значение другого.
MAX_NAME_TAIL = 24


def make_placeholder(name: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{name}"


def is_placeholder(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PLACEHOLDER_PREFIX)


def suggest_secret_name(project_id: int, item: AccessItem) -> str:
    """Имя переменной окружения для пункта чеклиста.

    Уже проставленный `secret_ref` сильнее любой генерации: под этим именем
    ключ может уже лежать в промптах и в коде проекта.
    """
    existing = refs_in(item.secret_ref or "")
    if existing:
        return existing[0]
    tail = "".join(_TRANSLIT.get(ch, ch) for ch in (item.name or "").lower())
    tail = _NON_WORD.sub("_", tail).strip("_").upper()[:MAX_NAME_TAIL].strip("_")
    kind = _NON_WORD.sub("_", (item.kind or "other")).strip("_").upper()
    parts = [f"P{project_id}", kind, tail, str(item.id)]
    return "_".join(part for part in parts if part)


# ---------- включение и выключение ----------

async def enable(project_id: int, vault=None) -> dict:
    """Включить режим: недостающим пунктам — заглушки, гейт снят.

    Пункты, которые уже `verified`, не трогаются: настоящий ключ заглушкой
    не подменяется ни при каких обстоятельствах.
    """
    v = vault or default_vault
    filled: list[str] = []
    async with Session() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        items = (await s.execute(
            select(AccessItem).where(AccessItem.project_id == project_id,
                                     AccessItem.status != "verified",
                                     AccessItem.stale.is_(False)))).scalars().all()
        for item in items:
            name = suggest_secret_name(project_id, item)
            item.secret_ref = "{{SECRET:%s}}" % name
            item.status = PLACEHOLDER
            if v.enabled and not v.get(name):
                v.set(name, make_placeholder(name))
            filled.append(name)
        project.autonomous_mode = True
        project.last_action = "режим самостоятельности: работаю с заглушками"
        project.updated_at = utcnow()
        await s.commit()

    log.warning("проект %s: режим самостоятельности ВКЛЮЧЁН, заглушек %s (%s). "
                "Гейт доступов снят; проверки, которым нужен настоящий ключ, "
                "выполняться не будут", project_id, len(filled), ", ".join(filled))
    return {"placeholders": filled}


async def disable(project_id: int) -> dict:
    """Выключить режим. Заглушки из vault НЕ удаляются.

    Удалить их значило бы оставить `secret_ref` пунктов указывающим в пустоту,
    а следом получить у агента переменную окружения без значения. Пункты
    возвращаются в `needed`, и гейт снова их считает.
    """
    async with Session() as s:
        project = await s.get(Project, project_id)
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        items = (await s.execute(
            select(AccessItem).where(AccessItem.project_id == project_id,
                                     AccessItem.status == PLACEHOLDER))).scalars().all()
        for item in items:
            item.status = "needed"
        project.autonomous_mode = False
        project.updated_at = utcnow()
        await s.commit()
    log.warning("проект %s: режим самостоятельности выключен, пунктов вернулось "
                "в ожидание: %s", project_id, len(items))
    return {"returned": len(items)}


# ---------- настоящий ключ вместо заглушки ----------

async def put_key(project_id: int, item_id: int, value: str, vault=None) -> dict:
    """Владелец вставил настоящее значение доступа.

    Пункт становится `verified` — то же самое, что делает рука человека
    в `tasks_cli`: автопроверки доступа у нас нет и не появилось, `verified`
    по-прежнему ставит человек, просто теперь одним движением.

    Задачи, ждавшие этого ключа, возвращаются НА ПРИЁМКУ, а не в готовые:
    ключ появился — значит проверку наконец можно выполнить по-настоящему.
    """
    v = vault or default_vault
    if not str(value).strip():
        raise ValueError("пустое значение — это не доступ")
    if is_placeholder(value):
        raise ValueError("это заглушка, а не настоящее значение")
    if not v.enabled:
        raise ValueError("VAULT_KEY не задан — класть значение некуда")

    async with Session() as s:
        item = await s.get(AccessItem, item_id)
        if item is None or item.project_id != project_id:
            raise ValueError(f"пункта {item_id} у проекта {project_id} нет")
        name = suggest_secret_name(project_id, item)
        v.set(name, value)
        item.secret_ref = "{{SECRET:%s}}" % name
        item.status = "verified"
        item.verified_at = utcnow()
        item.note = (item.note or "") and item.note
        await s.commit()

    resumed = await resume_tasks(project_id, name)
    log.info("проект %s: доступ «%s» получен, секрет %s записан, задач вернулось "
             "на приёмку: %s", project_id, item.name, name, resumed)
    return {"secret": name, "resumed": resumed}


async def resume_tasks(project_id: int, secret_name: str) -> int:
    """Вернуть на приёмку задачи, которые ждали именно этот ключ.

    Ждали именно его, а не «какой-нибудь»: у проекта может не хватать трёх
    ключей сразу, и первый же вставленный отправил бы на переприёмку все
    задачи разом — включая те, что провалятся на двух оставшихся заглушках.
    """
    moved = 0
    async with Session() as s:
        tasks = (await s.execute(
            select(Task).where(Task.project_id == project_id,
                               Task.status == AWAITING_KEYS))).scalars().all()
        for task in tasks:
            if secret_name not in waiting_for(task):
                continue
            task.status = "ready"
            task.lane = "verify"
            task.updated_at = utcnow()
            moved += 1
        if moved:
            await s.commit()
    return moved


def waiting_for(task: Task) -> list[str]:
    """Имена ключей, которых задаче не хватило. Пишутся в `last_error`
    при откладывании — отдельной колонки ради этого не заводим."""
    return refs_in(task.last_error or "")


# ---------- что видит приёмка ----------

def placeholder_names(vault=None) -> set[str]:
    """Имена секретов, за которыми сейчас стоит заглушка."""
    v = vault or default_vault
    if not v.enabled:
        return set()
    try:
        return {name for name in v.names() if is_placeholder(v.get(name))}
    except RuntimeError:
        return set()


def secrets_in_check(check: dict) -> list[str]:
    """На какие секреты ссылается сам критерий приёмки.

    Ищем обе формы: `{{SECRET:NAME}}` — так пишет чеклист, и `$NAME` — так
    ссылка выглядит после `to_env_names()`, то есть ровно так её видит агент
    и, скорее всего, напишет планировщик в shell-команде.
    """
    blob = " ".join(str(v) for v in (check or {}).values() if isinstance(v, (str, int)))
    found = list(refs_in(blob))
    for match in re.findall(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?", blob):
        if match not in found:
            found.append(match)
    return found


def deferred_reason(check: dict, blocked: set[str]) -> str | None:
    """Почему эту проверку нельзя выполнить: какого настоящего ключа нет.

    None — выполнять можно как обычно.
    """
    if not blocked:
        return None
    need = [n for n in secrets_in_check(check) if n in blocked]
    if not need:
        return None
    return ("проверка не выполнена: нужен настоящий ключ "
            + ", ".join("{{SECRET:%s}}" % n for n in need))


def placeholders_in_output(text: str) -> list[str]:
    """Имена заглушек, встретившихся в выводе проверки.

    Значение заглушки самоописывающееся (`PLACEHOLDER::ИМЯ`) именно ради
    этого: по строке в выводе видно не только что ключ фальшивый, но и какой.
    """
    return list(dict.fromkeys(
        re.findall(re.escape(PLACEHOLDER_PREFIX) + r"([A-Za-z0-9_.-]+)", text or "")))


def output_hit_placeholder(text: str) -> bool:
    """В выводе проверки встретился маркер заглушки.

    Второй признак, и он ловит неявный случай: критерий про ключ ничего не
    говорит (`pytest tests/test_payments.py`), но код внутри читает переменную
    окружения и валится, напечатав её значение. Одного первого признака мало —
    планировщик пишет команды, а не ссылки на секреты.
    """
    return PLACEHOLDER_PREFIX in (text or "")


PROMPT_NOTE = """
# ВНИМАНИЕ: РЕЖИМ САМОСТОЯТЕЛЬНОСТИ
Ключи и доступы в окружении — ЗАГЛУШКИ вида PLACEHOLDER::ИМЯ, а не настоящие
значения. Клиент их ещё не прислал.

Что это значит для работы:
- пиши код, который читает ключ ИЗ ОКРУЖЕНИЯ по имени. Не хардкодь ни значение,
  ни заглушку: настоящий ключ подставят позже, и код должен заработать без правок;
- не считай задачу выполненной по факту того, что внешний сервис ответил на
  заглушку. Он ответит ошибкой, и это ожидаемо;
- где нужен живой вызов внешнего сервиса, которого сейчас не будет, — закрой
  его тестом на подставном ответе (мок/стаб) и честно скажи в отчёте, что живой
  вызов не проверялся;
- НЕ придумывай «временный» ключ и не отключай проверку подписи, чтобы обойти
  заглушку. Это ровно тот случай, когда обход дороже недоделки.
"""
