"""Видно ли, что происходит: пульс автопилота и причины простоя.

Два вопроса, на которые дашборд до этого не отвечал, а спрашивают их первыми.

**«Автопилот вообще работает?»** Дашборд — отдельный процесс, он только
читает базу. Пока `python -m autopilot.main` не запущен, страница выглядит
ровно так же: заказы на месте, задачи «в очереди», кнопки нажимаются — и
ничего не происходит. Отличить это состояние было нельзя вообще никак.
Поэтому планировщик на каждом тике оставляет пульс, а дашборд смотрит,
насколько он свежий.

**«Почему заказ стоит?»** Гейтов у полосы `build` четыре (статус проекта,
галочка, бриф, доступы), плюс общие тормоза: пауза, суточные потолки, упор
в квоту, тихие часы. Закрыт может быть любой, а видно было только «в очереди».
`blockers()` перечисляет их поимённо — по одному и тому же проекту это часто
не один пункт, и сняв первый, человек справедливо ждал бы, что поедет.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from .config import cfg

log = logging.getLogger("progress")

# Пульс старше этого — считаем, что автопилот не работает. Тик планировщика
# идёт раз в СЕКУНДУ (`Scheduler.tick_sec = 1.0`), так что запас тридцатикратный:
# пережить долгий проход по базе, но не пережить остановку процесса.
#
# Здесь стояло «по умолчанию 5 секунд, запас пятикратный» — карта разошлась
# с кодом, и следующий, кто добавит работу в тик, посчитал бы запас по неправде.
STALE_AFTER_SEC = 30


def pause_path():
    """Файл паузы. Один и тот же для дашборда и планировщика.

    Файл, а не поле в базе, намеренно: снять паузу человек должен уметь даже
    когда процесс автопилота лежит.
    """
    return cfg.root / "PAUSED"


def paused() -> bool:
    return pause_path().exists()


def pause_reason() -> str:
    """Почему стоит пауза, если тот, кто её поставил, это написал.

    Кнопка на дашборде пишет пустой файл, а `_policy_review` — строку про
    инцидент. Без чтения файла оба состояния выглядели одинаково: «работа
    на паузе» и кнопка «снять». Остановку по нарушению политики нельзя
    снимать одним щелчком, не прочитав, что произошло.
    """
    try:
        return pause_path().read_text(encoding="utf-8").strip()[:300]
    except OSError:
        return ""


def heartbeat_path():
    return cfg.logs / "heartbeat.json"


def write_heartbeat(payload: dict) -> None:
    """Пульс планировщика. Файл, а не строка в базе, по той же причине,
    что и PAUSED: он должен читаться, даже когда с базой что-то не так,
    и не должен добавлять записи в живую таблицу каждые пять секунд."""
    try:
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**payload, "at": _now_iso()},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.debug("пульс не записан: %s", e)


def read_heartbeat() -> dict:
    """Что известно о живости автопилота.

    `alive=False` — это НЕ поломка, а самое обычное состояние: процесс просто
    не запущен. Поэтому сообщение здесь не про ошибку, а про то, что сделать.
    """
    path = heartbeat_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alive": False, "age": None,
                "why": "автопилот ни разу не запускался в этой установке"}

    at = _parse(raw.get("at"))
    if at is None:
        return {"alive": False, "age": None, "why": "пульс нечитаем"}
    age = (_utcnow() - at).total_seconds()
    # Отдаём и весь снимок целиком: тихие часы, занятость полос, упор в квоту —
    # всё это планировщик уже посчитал, и пересчитывать это в дашборде значило
    # бы завести второе мнение о том же самом
    return {
        **raw,
        "alive": age <= STALE_AFTER_SEC,
        "age": round(age),
        "why": ("" if age <= STALE_AFTER_SEC else
                f"последний признак жизни {_human_age(age)} назад — "
                f"процесс остановлен"),
    }


def _human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} с"
    if seconds < 5400:
        return f"{seconds / 60:.0f} мин"
    return f"{seconds / 3600:.1f} ч"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


def _parse(value) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


# ---------- почему заказ стоит ----------

# Порядок важен: сначала то, что перекрывает всё остальное. Снять четвёртую
# причину, не сняв первую, — потерянное время.
def blockers(project, *, access_waiting: int, blocking_questions: int,
             open_tasks: int, agent_tasks: int) -> list[dict]:
    """Что мешает этому заказу двигаться. Пустой список — ничего не мешает.

    `fix` — что сделать человеку. Без него список превращается в диагноз без
    лечения: «brief_ready=False» ничего не говорит тому, кто не читал код.
    """
    out: list[dict] = []

    if project.status == "done":
        return [{"what": "заказ сдан", "fix": "", "kind": "done"}]
    if project.status == "blocked":
        out.append({
            "kind": "blocked",
            "what": "заказ заблокирован — так бывает после провала планировщика "
                    "или несобранного ТЗ",
            "fix": "разобраться с причиной и нажать «Вернуть в работу»",
        })
    if not project.ready_for_work:
        out.append({
            "kind": "ready_for_work",
            "what": "не стоит галочка «готов к работе»",
            "fix": "нажать «Готов к работе» — это подтверждение владельца, "
                   "что заказ можно брать",
        })
    if not project.brief_ready:
        detail = (f"осталось блокирующих вопросов: {blocking_questions}"
                  if blocking_questions else
                  "уверенности в понимании ТЗ не хватает")
        out.append({
            "kind": "brief",
            "what": f"ТЗ не собрано — {detail}",
            "fix": "ответить на вопросы клиента и пересобрать ТЗ. Это НЕ то же "
                   "самое, что нехватка ключей: строить по непонятому ТЗ значит "
                   "сделать не то и заплатить за это",
        })
    if access_waiting:
        out.append({
            "kind": "access",
            "what": f"ждём доступы от клиента: {access_waiting}",
            "fix": "вставить значения в блоке выше — или включить режим "
                   "самостоятельности и работать с заглушками",
        })
    if open_tasks and not agent_tasks:
        out.append({
            "kind": "no_agent_tasks",
            "what": "нет ни одной задачи, которую может взять агент "
                    "(все ручные, human-класса или ждут зависимостей)",
            "fix": "смотреть план: такую работу делает человек",
        })
    if not open_tasks:
        out.append({
            "kind": "no_tasks",
            "what": "открытых задач нет",
            "fix": "составить план, если он ещё не собран",
        })
    return out


def global_blockers(*, paused: bool, alive: bool, over_money: bool,
                    over_cli: bool, limited_until: float,
                    build_allowed: bool, over_sessions: bool = False,
                    install: list | None = None) -> list[dict]:
    """Что остановило работу целиком, независимо от заказов.

    У каждого пункта обязательна пара «что» и «что с этим делать»: пункт
    без второго — это жалоба, а не диагностика.
    """
    out: list[dict] = []
    # Чего не хватает самой установке. Блокирующее — первым пунктом намеренно:
    # пока нет git, судья получает ПУСТОЙ диф и честно судит пустоту — молча,
    # без ошибки, и все остальные объяснения простоя будут про другое.
    #
    # НЕблокирующее (отсутствие браузера) сюда не идёт вовсе: работу оно
    # не останавливает, а в списке «почему всё стоит» выглядит причиной.
    # Такой пункт врёт человеку ровно так же, как пауза, которую планировщик
    # не читал: он ищет не там.
    for item in (install or []):
        if not getattr(item, "blocking", True):
            continue
        out.append({"kind": f"install_{item.key}",
                    "what": item.what, "fix": item.fix})
    if not alive:
        out.append({
            "kind": "not_running",
            "what": "автопилот не запущен — ничего не выполняется",
            "fix": "запустить: python -m autopilot.main",
        })
    if paused:
        why = pause_reason()
        out.append({
            "kind": "incident" if why else "paused",
            "what": f"работа остановлена: {why}" if why else "работа на паузе",
            "fix": ("разобраться, что произошло, и только потом снимать паузу"
                    if why else "снять паузу в шапке"),
        })
    if over_money:
        out.append({"kind": "budget",
                    "what": f"суточный потолок денег исчерпан "
                            f"(${cfg.daily_budget_usd})",
                    "fix": "поднять DAILY_BUDGET_USD или подождать сутки"})
    if over_cli:
        out.append({"kind": "budget_cli",
                    "what": f"суточный потолок подписки исчерпан "
                            f"(~${cfg.daily_cli_budget_usd})",
                    "fix": "поднять DAILY_CLI_BUDGET_USD или подождать сутки"})
    if over_sessions:
        out.append({"kind": "budget_sessions",
                    "what": f"суточный потолок ЧИСЛА СЕССИЙ исчерпан "
                            f"({cfg.daily_cc_sessions}) — единственный счётчик, "
                            f"который не врёт при оборванных вызовах",
                    "fix": "поднять DAILY_CC_SESSIONS или подождать сутки"})
    if limited_until > 0:
        out.append({"kind": "limit",
                    "what": f"упёрлись в квоту подписки, ждём "
                            f"{limited_until / 60:.0f} мин",
                    "fix": "ничего — само отпустит"})
    if not build_allowed:
        out.append({"kind": "quiet",
                    "what": f"тихие часы сборки "
                            f"({cfg.quiet_build_start}:00–{cfg.quiet_build_end}:00)",
                    "fix": "QUIET_HOURS_BUILD_START/END в .env"})
    return out
