"""Извлечение ТЗ из групповой переписки.

Первый модуль, где решения принимает модель, а не код. Отсюда весь его дизайн:
**коду принадлежит последнее слово**, модель только предлагает.

Три рубежа, каждый — программный, не «доверимся модели»:

1. **Evidence.** Любой пункт брифа ссылается на конкретные сообщения
   `(transport, chat_id, message_id)`. Код проверяет, что эти сообщения
   существуют И что они подтверждают требование одним из двух способов:

   * `origin="client"` — содержательная реплика самого клиента;
   * `origin="confirmed_proposal"` — предложение ВЛАДЕЛЬЦА, на которое клиент
     явно согласился. Владелец ведёт переговоры сам и часто предлагает
     решения: «давайте сделаем фильтр по бренду» — «да, давайте». Без этого
     вида evidence половина реально согласованных требований терялась бы.

   Голое согласие без предложения по-прежнему невалидно, а явный отказ
   («нет, не надо») не теряется молча — пункт уезжает в `out_of_scope`.
2. **Схема.** Ответ валидируется по структуре; при несоответствии — повтор
   с текстом ошибки, максимум `BRIEF_MAX_ATTEMPTS` попыток, дальше эскалация.
3. **Деньги.** Цену, сроки и объём работ бриф не извлекает никогда. Это зона
   менеджера. Запрет продублирован постфильтром: даже если модель принесёт
   такой пункт, код его выкинет.

Плюс проверка перед вызовом API: если в промпте оказалось хоть одно значение
из vault — запрос не уходит вообще.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
from difflib import SequenceMatcher

from sqlalchemy import select

from .config import cfg
from .db import AccessItem, ChatMessage, Project, Run, Session, Task, utcnow
from .roles import CLIENT, OWNER
from .verifier import parse_judge_json
from .vault import MIN_MASKABLE_LEN, anthropic_key, missing_secret_message, ref, refs_in
from .vault import vault as default_vault

log = logging.getLogger("brief")

ORIGIN_CLIENT = "client"
ORIGIN_CONFIRMED = "confirmed_proposal"

# Сообщение считается длинным — то есть содержательным — с этого размера.
# Именно длинные несут ТЗ, поэтому под нож они идут последними, а не первыми.
LONG_MESSAGE_CHARS = 400

# Грубая оценка: для русского текста ~3 символа на токен. Точный счёт требовал
# бы токенизатора, а нам нужен порядок величины для бюджета контекста.
CHARS_PER_TOKEN = 3


def _tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN) + 1


# Модель сообщает в unreadable[], что текст до неё дошёл обрезанным. Это НАША
# поломка, а не свойство данных: вложение прочитать нельзя объективно, а текст
# мы обрубили сами. Прогон с такой пометкой не принимается.
TRUNCATION_KINDS = ("text_truncated", "truncated", "text_cut", "message_truncated")


def truncation_complaints(data: dict) -> list[str]:
    """Сообщения, про которые модель сказала «текст обрезан».

    Отличать от нечитаемых вложений обязательно. «Не вижу, что на фото» —
    честное ограничение, с ним живём. «Текст обрезан» — наш промах нарезки,
    и по обрубленной переписке ТЗ собирать нельзя.
    """
    out = []
    for item in (data or {}).get("unreadable") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind in TRUNCATION_KINDS or "truncat" in kind or "обрез" in kind:
            out.append(str(item.get("message_id") or "?"))
    return out

# Согласие и отказ. Списки короткие намеренно: чем шире сеть, тем выше шанс
# принять вежливое «хорошо, я подумаю» за подтверждение требования.
#
# Маркеры разделены на две группы, и это не придирка. «Нужен» в начале фразы
# почти всегда вводит ТРЕБОВАНИЕ («нужен интернет-магазин»), а согласием
# становится, только когда составляет всю реплику целиком («нужно»).
# Без такого разделения «нужен сайт» превращалось в голое согласие и
# переставало быть самостоятельным требованием клиента.
STRONG_AGREE_RE = re.compile(
    r"^\W*(да|ага|угу|ок|окей|okay|ok|хорошо|отлично|супер|согласн\w*|"
    r"давай(те)?|поддерживаю|верно|точно|именно|подходит|устраивает|"
    r"годится|принимается|утверждаю|запускаем)\b",
    re.IGNORECASE)
# эти считаются согласием, только если ими исчерпывается вся реплика
WEAK_AGREE_RE = re.compile(
    r"^\W*(нужно|нужен|нужна|нужны|надо|делайте|сделайте|делаем|можно)\W*$",
    re.IGNORECASE)
DISAGREE_RE = re.compile(
    r"^\W*(нет|не надо|не нужно|не нужен|не нужна|не будем|пока не|"
    r"давайте не|не стоит|откажемся|отказываемся|против|не подходит|"
    r"не устраивает|обойдёмся|обойдемся|лишнее|не хочу|не хотим)\b",
    re.IGNORECASE)

# Голое согласие — это «да», «ок», «давайте», и ничего больше. Если клиент
# написал «да, и ещё нужен блок отзывов», содержания там достаточно,
# чтобы считать реплику самостоятельным требованием.
BARE_AGREEMENT_MAX_WORDS = 4

# поля-списки, каждый элемент которых обязан нести evidence
LIST_FIELDS = ("deliverables", "stack", "constraints", "assets",
               "access_needed", "open_questions", "out_of_scope")
ACCESS_KINDS = ("ftp", "ssh", "git", "hosting_panel", "domain", "api_key",
                "analytics", "design", "content", "other")
# Модальность требования. Потеря «желательно» — это объём, за который
# никто не платил, поэтому у deliverables поле обязательное.
PRIORITIES = ("must", "should", "nice")

# Постфильтр: то, что бриф не имеет права извлекать ни при каких условиях.
# Цена и сроки — предмет разговора менеджера с клиентом, не наше дело.
#
# ВАЖНО про «оплату». Первая версия списка глушила любое «оплат», и на живом
# чате это выбросило центральное требование клиента: «чтобы люди сами оформляли
# заказ и оплачивали онлайн». Оплата картой — это ФУНКЦИЯ магазина, а не
# разговор о деньгах. Поэтому запрещаем коммерческие обороты и суммы,
# а не корень слова.
FORBIDDEN_RE = re.compile(
    r"цен[аыуе]\b|ценообразов|стоимост|сколько стоит|во сколько обойд|"
    r"бюджет|скидк|предоплат|аванс\b|рассрочк|смет[аыу]|тариф|прайс|гонорар|"
    r"деньг|доплат|счёт на оплату|счет на оплату|"
    r"порядок оплаты|услови[яй] оплаты|оплат[аыу] работ|"
    r"\d[\d\s]{2,}\s*(?:тенге|руб|₸|₽|долл|тыс)|\$\s*\d|"
    r"дедлайн|срок[иеа]?\b|успе[тюе]|"
    r"за\s+\d+\s+(?:недел|дн|месяц)|через\s+\d+\s+(?:недел|дн|месяц)|"
    r"к\s+\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE)

SYSTEM = """Ты аналитик. Из переписки рабочей группы ты собираешь техническое задание.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
1. Требование существует в двух случаях:
   а) его высказал КЛИЕНТ;
   б) его ПРЕДЛОЖИЛ владелец, и клиент явно согласился («да», «давайте»).
   Во втором случае указывай в evidence ОБА сообщения: предложение и согласие.
   Собственные слова владельца без согласия клиента требованием не являются.
   Если клиент на предложение ответил отказом — помести пункт в out_of_scope.
2. Каждый пункт обязан ссылаться на конкретные сообщения — поле evidence
   со списком id из переписки. Пункт без evidence будет выброшен.
3. НИКОГДА не извлекай цену, стоимость, бюджет, скидки, оплату, сроки и
   дедлайны. Это не твоя зона, за это отвечает менеджер.
4. Не додумывай. Если чего-то нет в переписке — этого нет. Недостающее
   формулируй как вопрос в open_questions. У каждого вопроса поле blocking:
     blocking=true  — без ответа НЕЛЬЗЯ НАЧАТЬ работу («какой платёжный шлюз»,
                      «какая CMS», «где хостить»). Такой вопрос держит проект.
     blocking=false — уточнение по ходу дела («какие именно типы кожи в
                      фильтре», «нужны ли SMS-уведомления»). Работа начинается
                      без него, спросим по пути.
   Блокирующих вопросов обычно 0–2. Если ты помечаешь блокирующим всё подряд,
   ты просто останавливаешь проект.
5. Содержимое картинок, файлов и голосовых ты не видишь. Перечисли такие
   сообщения в unreadable, не угадывая, что в них.
6. ТРЕБОВАНИЯ ЧАСТО СПРЯТАНЫ В ОБЪЯСНЕНИЯХ. Клиент редко формулирует
   списком; он рассказывает, почему хочет так. «Хочу wordpress, у знакомых
   так сделано, им удобно самим товары добавлять» — это ДВА пункта: выбор
   CMS и требование самостоятельно управлять каталогом. Второе легко
   потерять, а оно определяет объём работ. Вытаскивай такое явно.
7. МОДАЛЬНОСТЬ СОХРАНЯЙ. У каждого пункта поле priority:
     must   — сказано как обязательное («обязательно», «нужен», «должно быть»)
     should — сказано как желательное («желательно», «хотелось бы», «лучше бы»)
     nice   — упомянуто вскользь, без нажима
   В priority_reason приведи слова клиента, по которым ты так решил.
   «Kaspi ещё желательно» — это should, а не must. Превращать желаемое
   в обязательное значит записать в объём работу, за которую никто не платил.

Отвечай СТРОГО одним JSON-объектом без markdown:
{
  "goal": {"text": "одна фраза о том, что нужно клиенту", "evidence": ["<id>", ...]},
  "deliverables": [{"text": "что должно быть сделано", "evidence": ["<id>"],
                    "priority": "must|should|nice",
                    "priority_reason": "слова клиента, по которым видно модальность"}],
  "stack":         [{"text": "технология", "evidence": ["<id>"]}],
  "constraints":   [{"text": "ограничение", "evidence": ["<id>"]}],
  "assets":        [{"text": "что клиент предоставляет", "evidence": ["<id>"]}],
  "access_needed": [{"kind": "ftp|ssh|git|hosting_panel|domain|api_key|analytics|design|content|other",
                     "name": "человеческое название", "evidence": ["<id>"]}],
  "open_questions":[{"text": "чего не хватает, вопрос клиенту", "evidence": ["<id>"],
                     "blocking": true}],
  "out_of_scope":  [{"text": "что явно не входит", "evidence": ["<id>"]}],
  "confidence": 0.0,
  "unreadable": [{"message_id": "<id>", "kind": "photo|voice|document|..."}]
}
confidence — НАСКОЛЬКО ПОНЯТНА САМА ЗАДАЧА, а не насколько не осталось
вопросов. Это разные вещи, и их постоянно путают.
  0.9 — ясно, что и зачем делать; открытые вопросы есть, но это детали
        реализации, а не непонимание задачи
  0.6 — общая картина есть, но существенная часть объёма под вопросом
  0.3 — из переписки непонятно, что вообще требуется
Наличие open_questions само по себе confidence НЕ снижает: за блокировку
проекта отвечает поле blocking у вопросов, а не эта цифра."""


class SecretLeak(RuntimeError):
    """В промпт попало значение из хранилища. Запрос не отправляется."""


class BriefFailed(RuntimeError):
    """Модель дважды не смогла отдать валидный ответ."""


def empty_brief() -> dict:
    data = {"goal": None, "confidence": 0.0, "unreadable": []}
    for f in LIST_FIELDS:
        data[f] = []
    return data


def assert_no_secrets(prompt: str, vault=None) -> None:
    v = vault or default_vault
    if not v.enabled:
        return
    for value in v.values():
        if len(value) >= MIN_MASKABLE_LEN and value in prompt:
            raise SecretLeak("в промпте оказалось значение из хранилища")


def _msg_key(m: ChatMessage) -> str:
    return f"{m.transport}:{m.chat_id}:{m.tg_message_id}"


def agreement(text: str) -> str | None:
    """«да» -> "yes", «нет, не надо» -> "no", всё остальное -> None."""
    t = (text or "").strip()
    if not t:
        return None
    if DISAGREE_RE.match(t):
        return "no"
    if STRONG_AGREE_RE.match(t):
        return "yes"
    if WEAK_AGREE_RE.match(t):
        return "yes"
    return None


def is_bare_agreement(text: str) -> bool:
    """Согласие без собственного содержания."""
    if agreement(text) is None:
        return False
    words = [w for w in re.split(r"\W+", (text or "")) if w]
    return len(words) <= BARE_AGREEMENT_MAX_WORDS


def find_confirmations(messages: list[ChatMessage],
                       window: int | None = None) -> dict[str, tuple[str, str, str]]:
    """Ищет пары «предложение владельца → согласие клиента».

    Возвращает два индекса в одном словаре: по ключу предложения и по ключу
    согласия, значение — (ключ предложения, ключ согласия, вердикт).
    Модель может сослаться на любое из двух сообщений, и оба должны работать.

    Связь засчитывается, если согласие клиента либо является reply на
    предложение, либо идёт не дальше `window` сообщений после него и между
    ними не вклинилась реплика клиента на другую тему.
    """
    window = cfg.confirm_window if window is None else window
    by_msg_id = {(m.transport, m.chat_id, m.tg_message_id): m for m in messages}
    out: dict[str, tuple[str, str, str]] = {}

    for i, m in enumerate(messages):
        if m.sender_role != CLIENT:
            continue
        verdict = agreement(m.text)
        if verdict is None:
            continue

        proposal = None
        # 1) явный reply — самая надёжная связь, окно не применяем и содержание
        # согласия не ограничиваем: клиент прямо указал, на что отвечает
        if m.reply_to:
            cand = by_msg_id.get((m.transport, m.chat_id, str(m.reply_to)))
            if cand is not None and cand.sender_role == OWNER and (cand.text or "").strip():
                proposal = cand

        # 2) иначе смотрим назад в пределах окна — но только для ГОЛОГО согласия.
        # «Хорошо. Сайт хочу на wordpress» начинается с «хорошо», однако это не
        # подтверждение предыдущего предложения, а собственное требование
        # клиента. Без этого условия любое вежливое начало реплики задним
        # числом утверждало бы всё, что мы успели предложить
        if proposal is None and is_bare_agreement(m.text):
            for step, j in enumerate(range(i - 1, -1, -1), start=1):
                if step > window:
                    break
                prev = messages[j]
                if prev.sender_role == OWNER and (prev.text or "").strip():
                    proposal = prev
                    break
                if prev.sender_role == CLIENT and agreement(prev.text) is None:
                    # клиент успел заговорить о другом — связь разорвана
                    break

        if proposal is None:
            continue
        pair = (_msg_key(proposal), _msg_key(m), verdict)
        out[_msg_key(proposal)] = pair
        out[_msg_key(m)] = pair
    return out


def _norm_evidence(raw) -> list[str]:
    """Модель может отдать evidence строкой, числом или объектом — приводим к ключам."""
    out = []
    for item in (raw if isinstance(raw, list) else [raw]):
        if isinstance(item, dict):
            key = ":".join(str(item.get(k, "")) for k in ("transport", "chat_id", "message_id"))
            out.append(key.strip(":"))
        elif item is not None:
            out.append(str(item))
    return [o for o in out if o]


class Brief:
    def __init__(self, client=None, vault=None, communicator=None, model: str | None = None,
                 samples: int | None = None):
        self._client = client
        self.vault = vault or default_vault
        self.communicator = communicator
        self.model = model or cfg.brief_model
        # сколько раз собрать бриф за проход; >1 лечит разброс модели
        self.samples = cfg.brief_samples if samples is None else samples

    # ---------- сбор материала ----------

    async def _messages(self, project: Project) -> list[ChatMessage]:
        async with Session() as s:
            return (await s.execute(
                select(ChatMessage)
                .where(ChatMessage.project_id == project.id,
                       ChatMessage.deleted.is_(False))
                .order_by(ChatMessage.created_at, ChatMessage.id))).scalars().all()

    @staticmethod
    def _line(m: ChatMessage, body: str | None = None) -> str:
        text = m.text if body is None else body
        text = text or ""
        if m.has_media:
            text = (text + f" [ВЛОЖЕНИЕ: {m.media_kind or 'файл'}, "
                           f"содержимое недоступно]").strip()
        return f"[{_msg_key(m)}] {m.sender_role}: {text}"

    def _render(self, messages: list[ChatMessage]) -> tuple[str, list[str]]:
        """Переписка для промпта. Возвращает (текст, id обрезанных сообщений).

        Бюджет считается в ТОКЕНАХ. Прежняя версия считала сообщения и резала
        всё, что старше последних 80, до 120 символов — и на первой живой
        переписке выбросила ровно то, ради чего всё затевалось: заполненный
        клиентом бриф. Он был длинным, а значит и самым ценным.

        Отсюда правило: **длину сообщения нельзя использовать как признак
        неважности, она означает обратное**. Когда бюджет всё-таки жмёт,
        жертвуем короткими и старыми репликами («ок», «спасибо», «добрый
        день»), а длинные сохраняем целиком. Если и этого мало — длинные
        уходят на сжатие отдельным дешёвым вызовом (см. `_condense`),
        а не под нож.

        Второй элемент возврата — список сообщений, которые всё же пришлось
        урезать. Пустой он или нет, решает, принимать ли бриф вообще:
        обрезанный НАМИ текст это дефект прогона, а не свойство данных.
        """
        budget = cfg.brief_context_tokens
        if _tokens(sum(len(m.text or "") for m in messages)) <= budget:
            # обычный случай: переписка целиком влезает, резать нечего
            return "# Переписка\n" + "\n".join(self._line(m) for m in messages), []

        # Не влезло. Считаем, сколько занимают длинные — их трогаем последними
        long_ids = {m.id for m in messages if len(m.text or "") >= LONG_MESSAGE_CHARS}
        long_cost = _tokens(sum(len(m.text or "") for m in messages if m.id in long_ids))

        kept: dict[int, str] = {}
        spent = long_cost
        # короткие набираем с конца: свежие важнее старых при равной длине
        for m in reversed([m for m in messages if m.id not in long_ids]):
            cost = _tokens(len(m.text or "") + 40)
            if spent + cost > budget:
                continue
            kept[m.id] = m.text or ""
            spent += cost

        dropped_short = [m for m in messages if m.id not in long_ids and m.id not in kept]
        lines = ["# Переписка"]
        for m in messages:
            if m.id in long_ids or m.id in kept:
                lines.append(self._line(m))
        if dropped_short:
            lines.append("")
            lines.append(f"# Опущено коротких реплик: {len(dropped_short)} "
                         f"(приветствия, подтверждения, уточнения по срокам)")
        log.warning("проект: переписка не влезла в %s токенов — опущено %s коротких реплик, "
                    "все длинные сохранены целиком", budget, len(dropped_short))
        return "\n".join(lines), []

    def build_prompt(self, project: Project, messages: list[ChatMessage],
                     previous: dict | None = None, rendered: str | None = None) -> str:
        parts = [f"# Проект\n{project.title} (клиент: {project.client})"]
        if previous:
            parts.append("# Предыдущая версия брифа — обнови её, не теряя подтверждённого\n"
                         + json.dumps(previous, ensure_ascii=False, indent=2))
        parts.append(rendered if rendered is not None else self._render(messages)[0])
        parts.append("Верни обновлённый бриф одним JSON-объектом.")
        return "\n\n".join(parts)

    async def render_context(self, messages: list[ChatMessage]) -> str:
        """Переписка для промпта, при нужде сжатая — но не обрубленная.

        Сжимаем ТОЛЬКО если длинные сообщения не влезают даже сами по себе.
        Тогда самые старые из них уходят на пересказ дешёвой моделью: пересказ
        теряет детали, но сохраняет смысл, а обрубание по счётчику символов
        не сохраняет ничего — оно рубит ровно посередине списка требований.
        """
        long_msgs = [m for m in messages if len(m.text or "") >= LONG_MESSAGE_CHARS]
        long_cost = _tokens(sum(len(m.text or "") for m in long_msgs))
        if long_cost <= cfg.brief_context_tokens:
            return self._render(messages)[0]

        log.warning("длинные сообщения занимают ~%s токенов при бюджете %s — "
                    "самые старые уходят на сжатие", long_cost, cfg.brief_context_tokens)
        condensed: dict[int, str] = {}
        spent = long_cost
        for m in long_msgs:                       # от старых к свежим
            if spent <= cfg.brief_context_tokens:
                break
            summary = await self._condense(m)
            if not summary:
                continue
            spent -= _tokens(len(m.text or "") - len(summary))
            condensed[m.id] = summary

        lines = ["# Переписка"]
        for m in messages:
            if m.id in condensed:
                lines.append(self._line(m, "[ПЕРЕСКАЗ, исходное сообщение длиннее] "
                                           + condensed[m.id]))
            else:
                lines.append(self._line(m))
        return "\n".join(lines)

    async def _condense(self, m: ChatMessage) -> str | None:
        """Пересказ одного длинного сообщения дешёвой моделью.

        Дешёвой намеренно: задача механическая — сжать, ничего не потеряв
        из требований. Платить за неё ценой основной модели незачем.
        """
        if self.client is None:
            return None
        try:
            resp = await self.client.messages.create(
                model=cfg.coverage_model, max_tokens=1200,
                messages=[{"role": "user", "content":
                           "Перескажи сообщение, сохранив ВСЕ требования, числа, "
                           "названия и перечисления. Ничего не додумывай.\n\n"
                           + (m.text or "")}])
            return "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text").strip() or None
        except Exception:
            log.exception("не смог сжать сообщение %s — оставляю как есть", m.id)
            return None

    # ---------- вызов модели ----------

    @property
    def client(self):
        """Ключ ищется в трёх местах: vault, окружение, .env — см. resolve_secret."""
        if self._client is None:
            key = anthropic_key(self.vault)
            if key:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=key)
        return self._client

    async def _call(self, prompt: str, task_id: int | None, project_id: int) -> tuple[str, str]:
        # ПОСЛЕДНИЙ рубеж перед сетью: секрет не должен уехать в чужой дата-центр
        assert_no_secrets(prompt, self.vault)

        if self.client is None:
            raise BriefFailed(missing_secret_message("ANTHROPIC_API_KEY"))

        t0 = time.monotonic()
        resp = await self.client.messages.create(
            model=self.model, max_tokens=cfg.brief_max_tokens, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        await self._charge(resp, time.monotonic() - t0, task_id, project_id)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, str(getattr(resp, "stop_reason", "") or "")

    async def _charge(self, resp, seconds: float, task_id: int | None, project_id: int) -> None:
        from .verifier import PRICE_IN, PRICE_OUT
        usage = getattr(resp, "usage", None)
        cost = 0.0
        tokens_in = tokens_out = 0
        if usage is not None:
            tokens_in = getattr(usage, "input_tokens", 0) or 0
            tokens_out = getattr(usage, "output_tokens", 0) or 0
            cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
        log.info("бриф: вход %s токенов, выход %s токенов, $%.4f, %.1fs",
                 tokens_in, tokens_out, cost, seconds)
        async with Session() as s:
            if task_id is None:
                # Run привязан к задаче: заводим служебную, иначе стоимость
                # брифа не попадёт в суточный бюджет
                t = Task(project_id=project_id, lane="chat", title="сбор ТЗ",
                         status="done", cost_usd=cost)
                s.add(t)
                await s.flush()
                task_id = t.id
            s.add(Run(task_id=task_id, kind="brief", ok=True,
                      cost_usd=cost, seconds=seconds))
            p = await s.get(Project, project_id)
            if p is not None:
                p.cost_usd += cost
            await s.commit()

    # ---------- валидация ----------

    def validate_schema(self, data) -> list[str]:
        errors = []
        if not isinstance(data, dict):
            return ["ответ не является JSON-объектом"]
        goal = data.get("goal")
        if goal is not None and not (isinstance(goal, dict) and isinstance(goal.get("text"), str)):
            errors.append("goal должен быть объектом с полем text либо null")
        for f in LIST_FIELDS:
            value = data.get(f, [])
            if not isinstance(value, list):
                errors.append(f"{f} должен быть списком")
                continue
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    errors.append(f"{f}[{i}] должен быть объектом")
                    continue
                if f == "access_needed":
                    if not isinstance(item.get("name"), str) or not item["name"].strip():
                        errors.append(f"{f}[{i}].name обязателен")
                    if item.get("kind") not in ACCESS_KINDS:
                        errors.append(f"{f}[{i}].kind должен быть одним из {ACCESS_KINDS}")
                elif not isinstance(item.get("text"), str) or not item["text"].strip():
                    errors.append(f"{f}[{i}].text обязателен")
                # Модальность требуем только у deliverables: именно там живёт
                # объём работ, и именно его нельзя раздувать молча
                if f == "open_questions" and not isinstance(item.get("blocking"), bool):
                    errors.append(
                        f"{f}[{i}].blocking обязателен: true, если без ответа "
                        f"нельзя начать работу, иначе false")
                if f == "deliverables" and item.get("priority") not in PRIORITIES:
                    errors.append(
                        f"{f}[{i}].priority обязателен и должен быть одним из {PRIORITIES}")
        conf = data.get("confidence")
        if not isinstance(conf, (int, float)) or not 0 <= float(conf) <= 1:
            errors.append("confidence должен быть числом от 0 до 1")
        if not isinstance(data.get("unreadable", []), list):
            errors.append("unreadable должен быть списком")
        return errors

    def apply_evidence(self, data: dict, messages: list[ChatMessage]) -> tuple[dict, list[str]]:
        """Оставляет только подтверждённое. Два законных вида подтверждения:

        * содержательная реплика клиента (origin="client");
        * предложение владельца + явное согласие клиента
          (origin="confirmed_proposal").

        Отказ клиента — не потеря: пункт переезжает в out_of_scope, чтобы
        было видно, что вопрос обсуждали и закрыли.
        """
        client_msgs = {_msg_key(m): m for m in messages if m.sender_role == CLIENT}
        # модель часто отдаёт голый message_id — принимаем и его, по всем ролям:
        # ссылка на предложение владельца теперь тоже законна
        short = {m.tg_message_id: _msg_key(m) for m in messages}
        known_any = set(short) | {_msg_key(m) for m in messages}
        confirmations = find_confirmations(messages)

        dropped: list[str] = []
        rejected: list[dict] = []

        def resolve(refs: list[str]) -> list[str]:
            return [short.get(r, r) for r in refs]

        def keep(item: dict, where: str) -> str:
            """Возвращает "keep", "reject" или "drop"."""
            refs = resolve(_norm_evidence(item.get("evidence")))

            # 1) обычный путь: клиент сказал это сам.
            # Голое «да» самостоятельным требованием не считается —
            # иначе любое согласие оправдывало бы что угодно
            substantive = [r for r in refs
                           if r in client_msgs and not is_bare_agreement(client_msgs[r].text)]
            if substantive:
                item["evidence"] = substantive
                item["origin"] = ORIGIN_CLIENT
                return "keep"

            # 2) подтверждённое предложение владельца
            for r in refs:
                pair = confirmations.get(r)
                if pair is None:
                    continue
                proposal_key, agree_key, verdict = pair
                item["evidence"] = [proposal_key, agree_key]
                item["origin"] = ORIGIN_CONFIRMED
                return "keep" if verdict == "yes" else "reject"

            reason = "ссылок нет"
            if refs and not any(r in known_any for r in refs):
                reason = f"выдуманные ссылки {refs}"
            elif refs and any(r in client_msgs for r in refs):
                reason = f"только голое согласие без предложения {refs}"
            elif refs:
                reason = f"не слова клиента и не подтверждённое предложение {refs}"
            dropped.append(f"{where}: {reason}")
            return "drop"

        out = dict(data)
        goal = out.get("goal")
        if isinstance(goal, dict):
            text = str(goal.get("text", ""))
            if FORBIDDEN_RE.search(text):
                dropped.append("goal: про деньги или сроки — не зона брифа")
                out["goal"] = None
            else:
                verdict = keep(goal, "goal")
                if verdict == "reject":
                    dropped.append("goal: клиент отказался — ушло в out_of_scope")
                    rejected.append(goal)
                    out["goal"] = None
                elif verdict == "drop":
                    out["goal"] = None

        for f in LIST_FIELDS:
            kept = []
            for i, item in enumerate(out.get(f) or []):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("name") or "")
                if FORBIDDEN_RE.search(text):
                    dropped.append(f"{f}[{i}]: про деньги или сроки — не зона брифа")
                    continue
                verdict = keep(item, f"{f}[{i}]")
                if verdict == "keep":
                    kept.append(item)
                elif verdict == "reject" and f != "out_of_scope":
                    dropped.append(f"{f}[{i}]: клиент отказался — ушло в out_of_scope")
                    rejected.append(item)
            out[f] = kept

        if rejected:
            merged = list(out.get("out_of_scope") or [])
            seen = {str(x.get("text") or x.get("name") or "").strip().lower() for x in merged}
            # одна и та же отклонённая пара не должна попадать в список дважды,
            # как бы модель ни назвала пункт
            seen_pairs = {tuple(x.get("evidence") or []) for x in merged}
            for item in rejected:
                text = str(item.get("text") or item.get("name") or "").strip()
                pair = tuple(item.get("evidence") or [])
                if text.lower() in seen or (pair and pair in seen_pairs):
                    continue
                seen.add(text.lower())
                seen_pairs.add(pair)
                merged.append({"text": text, "evidence": list(pair),
                               "origin": ORIGIN_CONFIRMED, "rejected": True})
            out["out_of_scope"] = merged

        out["confidence"] = float(out.get("confidence") or 0.0)
        out["unreadable"] = [u for u in (out.get("unreadable") or []) if isinstance(u, dict)]
        return out, dropped

    def collect_unreadable(self, data: dict, messages: list[ChatMessage]) -> dict:
        """Вложения фиксируем сами: полагаться на модель тут незачем,
        факт наличия файла виден из БД.

        Сверяем по ХВОСТУ идентификатора: модель отдаёт полный ключ
        `telegram:-100…:7`, а мы знаем голое `7`. Без нормализации одно и то же
        вложение попадает в список дважды — поймано на живом прогоне.
        """
        def tail(value) -> str:
            return str(value or "").rsplit(":", 1)[-1]

        seen = {tail(u.get("message_id")) for u in data.get("unreadable", [])}
        for m in messages:
            if m.has_media and tail(m.tg_message_id) not in seen:
                seen.add(tail(m.tg_message_id))
                data.setdefault("unreadable", []).append(
                    {"message_id": m.tg_message_id, "kind": m.media_kind or "файл"})
        return data

    # ---------- главный вход ----------

    async def build(self, project: Project) -> dict | None:
        messages = await self._messages(project)
        if not messages:
            log.info("проект %s: переписки нет, бриф собирать не из чего", project.id)
            return None

        previous = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
        meta = (project.brief or {}).get("_meta") if isinstance(project.brief, dict) else None
        meta = meta or {"last_id": 0, "since_full": 0}

        fresh = [m for m in messages if m.id > int(meta.get("last_id") or 0)]
        full = (previous is None
                or int(meta.get("since_full") or 0) + len(fresh) >= cfg.brief_full_rebuild_every)
        if not fresh and previous is not None:
            log.debug("проект %s: новых сообщений нет, бриф не пересобираю", project.id)
            return previous

        # инкрементально подаём предыдущий бриф + только новые реплики,
        # но проверять evidence всё равно надо по ВСЕЙ переписке
        window = messages if full else fresh
        rendered = await self.render_context(window)
        prompt = self.build_prompt(project, window, None if full else previous,
                                   rendered=rendered)

        samples: list[dict] = []
        dropped: list[str] = []
        rounds = max(1, self.samples)
        for i in range(rounds):
            try:
                raw = await self._attempt(prompt, project)
            except SecretLeak as e:
                log.critical("проект %s: %s — запрос к модели НЕ отправлен", project.id, e)
                await self._escalate(project, f"бриф остановлен: {e}")
                return None
            except BriefFailed as e:
                if samples:
                    # один прогон из нескольких сорвался — работаем на остальных
                    log.warning("проект %s: прогон %s/%s не удался: %s",
                                project.id, i + 1, rounds, e)
                    continue
                log.error("проект %s: бриф не собран — %s", project.id, e)
                await self._escalate(project, f"бриф не собран: {e}")
                return None

            cut = truncation_complaints(raw)
            if cut:
                # Модель СООБЩИЛА, что текст до неё дошёл обрезанным. Это наш
                # дефект, а не свойство данных: вложение объективно нечитаемо,
                # а текст мы обрубили сами. Принять такой бриф — значит принять
                # ТЗ, собранное по неполному условию, и не узнать об этом.
                log.critical("проект %s: модель сообщила об обрезанном тексте в %s "
                             "сообщениях (%s) — бриф НЕ принимается. Это дефект "
                             "нарезки контекста, а не переписки",
                             project.id, len(cut), ", ".join(cut[:8]))
                await self._notify(
                    f"Проект {project.id} «{project.title}». "
                    f"Бриф не собран: модель сообщила, что {len(cut)} сообщений "
                    f"дошли до неё обрезанными ({', '.join(cut[:5])}). Это поломка "
                    f"нарезки контекста на нашей стороне — ТЗ по обрубленной "
                    f"переписке собирать нельзя. Подними BRIEF_CONTEXT_TOKENS.")
                # Жёсткий стоп для ВСЕГО прогона, а не «сорвался один сэмпл».
                # Обрезка одинаково калечит все прогоны: контекст у них общий,
                # так что повторять на остальных бессмысленно
                await self._escalate(
                    project,
                    f"бриф не собран: текст {len(cut)} сообщений дошёл до модели "
                    f"обрезанным — это дефект нарезки контекста")
                return None

            checked, drops = self.apply_evidence(raw, messages)
            dropped.extend(drops)
            samples.append(checked)

        for reason in dropped:
            log.warning("проект %s: выброшен пункт — %s", project.id, reason)

        data = merge_samples(samples)
        data = trim_questions(data)
        data = self.collect_unreadable(data, messages)
        # Накапливаем ВСЕГДА, а не только на инкременте: модель недетерминирована,
        # и полный пересбор точно так же теряет пункты между прогонами
        data = accumulate(previous, data)

        await self._persist(project, data, messages, dropped, full=full)
        return data

    async def _attempt(self, prompt: str, project: Project) -> dict:
        last_errors: list[str] = []
        current = prompt
        for attempt in range(1, cfg.brief_max_attempts + 1):
            raw, stop_reason = await self._call(current, None, project.id)
            # тот же терпимый разбор, что у судьи: модель регулярно предваряет
            # JSON прозой, и строгий json.loads на этом давал ложный провал
            data, extracted = parse_judge_json(raw)
            if extracted:
                log.warning("проект %s: бриф начат прозой, JSON извлечён из текста",
                            project.id)
            if data is None:
                if stop_reason == "max_tokens":
                    # Отдельный внятный дефект. «Не-JSON» тут врёт: JSON был
                    # правильный, он просто не поместился в лимит ответа
                    last_errors = [f"ответ не поместился в {cfg.brief_max_tokens} токенов "
                                   f"и оборвался — подними BRIEF_MAX_TOKENS"]
                    log.error("проект %s: ответ модели обрезан по лимиту", project.id)
                else:
                    last_errors = ["ответ не разобрался как JSON"]
            else:
                last_errors = self.validate_schema(data)
                if not last_errors:
                    return data
            log.warning("проект %s: попытка %s не прошла валидацию: %s",
                        project.id, attempt, "; ".join(last_errors)[:300])
            current = (prompt + "\n\n# Предыдущий ответ отвергнут, исправь:\n- "
                       + "\n- ".join(last_errors))
        raise BriefFailed("; ".join(last_errors)[:300])

    async def _notify(self, text: str) -> None:
        """Уведомление владельцу без смены статуса проекта."""
        if self.communicator is None:
            return
        notify = getattr(self.communicator, "notify_owner", None)
        if notify is not None:
            await notify(text)

    async def _escalate(self, project: Project, text: str) -> None:
        async with Session() as s:
            p = await s.get(Project, project.id)
            if p is not None:
                p.status = "blocked"
                p.last_action = text[:300]
                p.brief_ready = False
                p.updated_at = utcnow()
                await s.commit()
        if self.communicator is not None:
            notify = getattr(self.communicator, "notify_owner", None)
            if notify is not None:
                await notify(f"Проект {project.id} «{project.title}»: {text}")

    # ---------- запись результата ----------

    async def _persist(self, project: Project, data: dict, messages: list[ChatMessage],
                       dropped: list[str], full: bool = True) -> None:
        # Полнота понимания и наличие вопросов — разные вещи. Проект держат
        # только вопросы, без ответа на которые нельзя начать, и низкая
        # уверенность в понимании задачи. Любое уточнение по ходу дела —
        # не повод стоять
        blockers = blocking_questions(data)
        ready = (bool(data.get("goal")) and not blockers
                 and float(data.get("confidence") or 0) >= cfg.brief_min_confidence)

        payload = {
            "brief": data,
            "_meta": {
                "last_id": max(m.id for m in messages),
                "since_full": 0,
                "hash": hashlib.sha256(
                    "|".join(_msg_key(m) for m in messages).encode()).hexdigest()[:16],
                "dropped": dropped[:20],
                "updated_at": utcnow().isoformat(),
            },
        }
        async with Session() as s:
            p = await s.get(Project, project.id)
            if p is None:
                return
            p.brief = payload
            p.brief_ready = ready
            p.updated_at = utcnow()
            await s.commit()
        project.brief = payload
        project.brief_ready = ready

        await self.sync_access_items(project, data, full=full, messages=messages)
        log.info("проект %s: бриф обновлён, confidence=%.2f, вопросов=%d "
                 "(блокирующих %d), готов=%s",
                 project.id, data.get("confidence", 0),
                 len(data.get("open_questions") or []), len(blockers), ready)

    @staticmethod
    def _already_sent(messages: list[ChatMessage]) -> dict[str, list[str]]:
        """Сообщения, в которых клиент уже прислал доступ.

        Признак прямой: перехватчик секретов оставил в тексте `{{SECRET:...}}`.
        Если пункт чеклиста ссылается на такое сообщение, доступ уже у нас,
        и заводить его в статусе `needed` — значит просить второй раз то,
        что клиент прислал десять сообщений назад.
        """
        out: dict[str, list[str]] = {}
        for m in messages:
            names = refs_in(m.text or "")
            if names:
                out[_msg_key(m)] = names
        return out

    async def sync_access_items(self, project: Project, data: dict, full: bool = True,
                                messages: list[ChatMessage] | None = None) -> None:
        """Пункты доступа из брифа. Дубли не плодим, verified не сбрасываем.

        `stale` проставляем ТОЛЬКО после полного пересбора. При инкрементальном
        проходе модель видит лишь новые реплики и физически не может повторить
        весь список — считать отсутствие пункта его отменой означало бы
        вычёркивать реальные потребности на ровном месте.
        """
        wanted = {}
        for item in data.get("access_needed") or []:
            key = (str(item.get("kind", "other")).lower(), str(item.get("name", "")).strip().lower())
            if key[1]:
                wanted[key] = item

        sent = self._already_sent(messages or [])

        async with Session() as s:
            existing = (await s.execute(
                select(AccessItem).where(AccessItem.project_id == project.id))).scalars().all()

            def find_existing(kind: str, name: str):
                """Ищем нечётко: модель называет один и тот же доступ
                по-разному, а точное сравнение плодило по строке на прогон.
                Чеклист держит полосу build — засорять его нельзя."""
                probe = {"kind": kind, "name": name}
                for row in existing:
                    if same_item({"kind": row.kind, "name": row.name}, probe):
                        return row
                return None

            for key, item in wanted.items():
                current = find_existing(key[0], str(item.get("name") or ""))
                # доступ уже приходил в чат — заводим сразу как полученный
                secret_names: list[str] = []
                for ref_key in item.get("evidence") or []:
                    secret_names.extend(sent.get(str(ref_key), []))
                received = bool(secret_names)

                if current is None:
                    s.add(AccessItem(
                        project_id=project.id, kind=key[0],
                        name=str(item.get("name")).strip(),
                        status="received" if received else "needed",
                        secret_ref=ref(secret_names[0]) if secret_names else None,
                        note="прислан в переписке" if received else "",
                        source="brief"))
                else:
                    if current.stale:
                        current.stale = False   # вернулся в бриф — снова актуален
                    if received and current.status == "needed":
                        # клиент прислал доступ уже после создания пункта
                        current.status = "received"
                        current.secret_ref = current.secret_ref or ref(secret_names[0])
                        current.note = current.note or "прислан в переписке"

            for current in existing:
                key = (current.kind.lower(), current.name.strip().lower())
                still_wanted = any(
                    same_item({"kind": current.kind, "name": current.name},
                              {"kind": k[0], "name": str(v.get("name") or "")})
                    for k, v in wanted.items())
                if not full or still_wanted or current.source != "brief":
                    continue
                # пункт пропал из брифа. Проверенный доступ не трогаем:
                # клиент его уже дал, и терять это нельзя
                if current.status != "verified":
                    current.stale = True
            await s.commit()


def item_text(item: dict) -> str:
    return str(item.get("text") or item.get("name") or "").strip()


def item_key(item: dict) -> str:
    """Ключ для сопоставления пунктов между прогонами."""
    return item_text(item).lower()


# Модель каждый раз формулирует иначе: «Корзина», «Корзина для оформления
# заказа», «Корзина и оформление заказа покупателем самостоятельно» — это
# один и тот же пункт. Сопоставление по точному тексту раздувало бриф втрое.
SIMILAR_ENOUGH = 0.75
# при пересечении evidence порог мягче: ссылка на то же сообщение —
# сильный довод, что речь об одном требовании
SIMILAR_WITH_EVIDENCE = 0.55


# Короткая формулировка целиком входит в длинную («Корзина» и «Корзина для
# оформления заказа») — это один пункт. Порог длины нужен, чтобы обрубок
# вроде «оплата» не склеивал оплату картой с оплатой через Kaspi.
# Вхождение проверяется только для КОРОТКОЙ строки внутри длинной,
# поэтому «оплата картой» и «оплата через Kaspi» остаются разными.
CONTAINED_MIN_LEN = 6


def same_item(a: dict, b: dict) -> bool:
    from .groups import normalize
    # Вид доступа проверяем ПЕРВЫМ: домен и панель хостинга — разные пункты,
    # даже если названы одинаково
    if a.get("kind") and b.get("kind") and a["kind"] != b["kind"]:
        return False

    ta, tb = normalize(item_text(a)), normalize(item_text(b))
    if not ta or not tb:
        return False
    if ta == tb:
        return True

    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= CONTAINED_MIN_LEN and short in long:
        return True

    ev_a, ev_b = set(a.get("evidence") or []), set(b.get("evidence") or [])
    shared = ev_a & ev_b
    same_evidence = bool(ev_a) and ev_a == ev_b

    # Два пункта чеклиста одного вида, ссылающиеся на одно сообщение, — это
    # один доступ, как бы по-разному модель его ни назвала
    if same_evidence and a.get("kind") and a.get("kind") == b.get("kind"):
        return True
    # Подтверждённое предложение опознаётся парой «предложение + согласие».
    # Одинаковая пара — одно и то же согласованное решение
    if (same_evidence and a.get("origin") == ORIGIN_CONFIRMED
            and b.get("origin") == ORIGIN_CONFIRMED):
        return True

    ratio = SequenceMatcher(None, ta, tb).ratio()
    if ratio >= SIMILAR_ENOUGH:
        return True

    # Порядок слов модель тасует свободно: «оплата картой онлайн» и
    # «онлайн-оплата банковской картой» — одно и то же. Посимвольное сравнение
    # этого не видит, поэтому смотрим ещё и на пересечение слов
    wa = {w for w in ta.split() if len(w) > 2}
    wb = {w for w in tb.split() if len(w) > 2}
    if wa and wb:
        overlap = len(wa & wb) / min(len(wa), len(wb))
        if overlap >= 0.85:
            return True
        if overlap >= 0.7 and shared:
            return True

    return ratio >= SIMILAR_WITH_EVIDENCE and bool(shared)


def _pick_priority(variants: list[dict]) -> tuple[str | None, str]:
    """Модальность по большинству прогонов. Расхождение показываем явно:
    молча выбрать сильную — это раздуть объём, слабую — потерять требование."""
    votes: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for v in variants:
        prio = v.get("priority")
        if prio in PRIORITIES:
            votes[prio] = votes.get(prio, 0) + 1
            reasons.setdefault(prio, str(v.get("priority_reason") or ""))
    if not votes:
        return None, ""
    best = max(votes, key=lambda k: (votes[k], -PRIORITIES.index(k)))
    reason = reasons.get(best, "")
    if len(votes) > 1:
        spread = ", ".join(f"{k}×{n}" for k, n in sorted(votes.items()))
        reason = f"{reason} [модальность расходится между прогонами: {spread}]".strip()
    return best, reason


def _fold(variants: list[dict]) -> dict:
    """Схлопывает разные формулировки одного пункта в одну запись."""
    base = dict(max(variants, key=lambda v: len(item_text(v))))
    evidence: list[str] = []
    for v in variants:
        for e in v.get("evidence") or []:
            if e not in evidence:
                evidence.append(e)
    base["evidence"] = evidence
    prio, reason = _pick_priority(variants)
    if prio:
        base["priority"] = prio
        if reason:
            base["priority_reason"] = reason
    if any(v.get("origin") == ORIGIN_CONFIRMED for v in variants):
        base["origin"] = ORIGIN_CONFIRMED
    if any(v.get("rejected") for v in variants):
        base["rejected"] = True
    return base


def merge_samples(samples: list[dict]) -> dict:
    """Объединяет N независимых прогонов модели по одному и тому же чату.

    Пункт входит в итог, если встретился хотя бы в одном прогоне. Модель
    недетерминирована: на живом чате два прогона подряд дали разный состав
    брифа, и потерянное требование не попало бы в план вообще.
    Сколько раз пункт встретился — видно в поле `samples`.
    """
    if not samples:
        return empty_brief()
    if len(samples) == 1:
        out = dict(samples[0])
        total = 1
    else:
        out = dict(samples[0])
        total = len(samples)

    for field in LIST_FIELDS:
        buckets: list[list[dict]] = []
        for data in samples:
            seen_here: list[int] = []
            for item in data.get(field) or []:
                if not isinstance(item, dict) or not item_text(item):
                    continue
                # сравниваем со ВСЕМИ формулировками группы, а не только с
                # первой: похожесть нетранзитивна, и A~B, B~C при A≁C
                # разваливало один пункт на два
                spot = next((i for i, group in enumerate(buckets)
                             if any(same_item(existing, item) for existing in group)), None)
                if spot is None:
                    buckets.append([item])
                    seen_here.append(len(buckets) - 1)
                else:
                    buckets[spot].append(item)
                    if spot not in seen_here:
                        seen_here.append(spot)
        folded = []
        for group in buckets:
            # считаем ПРОГОНЫ, а не варианты формулировок внутри одного прогона
            hits = sum(1 for data in samples
                       if any(isinstance(i, dict)
                              and any(same_item(existing, i) for existing in group)
                              for i in (data.get(field) or [])))
            item = _fold(group)
            item["samples"] = f"{hits}/{total}"
            folded.append(item)
        out[field] = folded

    goal = next((d.get("goal") for d in samples if d.get("goal")), None)
    out["goal"] = dict(goal) if goal else None
    if out["goal"]:
        hits = sum(1 for d in samples if d.get("goal"))
        out["goal"]["samples"] = f"{hits}/{total}"
    # confidence берём минимальную: разброс сам по себе повод не спешить
    out["confidence"] = min(float(d.get("confidence") or 0.0) for d in samples)
    unreadable: list[dict] = []
    seen_u = set()
    for data in samples:
        for u in data.get("unreadable") or []:
            key = str(u.get("message_id") or "").rsplit(":", 1)[-1]
            if key and key not in seen_u:
                seen_u.add(key)
                unreadable.append(u)
    out["unreadable"] = unreadable
    return out


def accumulate(previous: dict | None, fresh: dict, now: str | None = None) -> dict:
    """Бриф НАКАПЛИВАЕТ пункты, а не перезаписывается.

    Пункт, попавший в бриф хотя бы раз, молча не исчезает: если в новом
    прогоне его нет, он остаётся с пометкой `missing`. Потерянный отказ
    вернётся сам, а вот потерянное требование просто не попадёт в план,
    и этого никто не заметит.

    Единственный автоматический путь удаления — явный отказ клиента
    (пункт уезжает в out_of_scope в apply_evidence). Всё остальное убирает
    человек руками.
    """
    stamp = now or utcnow().isoformat()
    out = dict(fresh)
    previous = previous or {}

    for field in LIST_FIELDS:
        fresh_list = [dict(i) for i in (fresh.get(field) or [])
                      if isinstance(i, dict) and item_text(i)]
        old_list = [dict(i) for i in (previous.get(field) or [])
                    if isinstance(i, dict) and item_text(i)]
        # сопоставляем нечётко: между прогонами формулировка гуляет,
        # и по точному совпадению старый пункт выглядел бы пропавшим
        fresh_items = {item_key(i): i for i in fresh_list}
        old_items = {item_key(i): i for i in old_list}
        pair: dict[str, dict] = {}
        for old in old_list:
            match = next((f for f in fresh_list if same_item(old, f)), None)
            if match is not None:
                pair[item_key(old)] = match

        rejected_items = [i for i in (fresh.get("out_of_scope") or [])
                          if isinstance(i, dict) and i.get("rejected")]
        rejected = {item_key(i) for i in rejected_items}

        # Порядок сохраняем прежний: сначала то, что уже было (в исходном
        # порядке), потом новое. Бриф читают глазами, и перетасовка списка
        # на каждом прогоне мешает заметить, что именно изменилось.
        result: list[dict] = []
        used: set[int] = set()
        for old in old_list:
            key = item_key(old)
            match = pair.get(key)
            if match is not None:
                match["first_seen"] = old.get("first_seen") or stamp
                match["last_seen"] = stamp
                match["seen_count"] = int(old.get("seen_count") or 0) + 1
                match.pop("missing", None)
                result.append(match)
                used.add(id(match))
            elif field == "open_questions" or any(same_item(old, r) for r in rejected_items):
                continue        # закрытый вопрос и отвергнутый пункт не возвращаем
            else:
                kept = dict(old)
                kept["missing"] = True      # видно в brief_eval отдельным блоком
                kept.setdefault("first_seen", stamp)
                kept.setdefault("seen_count", 1)
                result.append(kept)

        for item in fresh_list:
            if id(item) in used:
                continue
            item["first_seen"] = stamp
            item["last_seen"] = stamp
            item["seen_count"] = 1
            item.pop("missing", None)
            result.append(item)
        out[field] = result

    goal = fresh.get("goal") or previous.get("goal")
    if goal:
        goal = dict(goal)
        old_goal = previous.get("goal") or {}
        goal["first_seen"] = old_goal.get("first_seen") or stamp
        goal["last_seen"] = stamp if fresh.get("goal") else old_goal.get("last_seen", stamp)
        goal["seen_count"] = int(old_goal.get("seen_count") or 0) + (1 if fresh.get("goal") else 0)
        if not fresh.get("goal"):
            goal["missing"] = True
    out["goal"] = goal
    out["confidence"] = float(fresh.get("confidence") or 0.0)
    return out


def merge(previous: dict, fresh: dict) -> dict:
    """Совместимость: инкрементальное обновление = накопление."""
    return accumulate(previous, fresh)


def missing_items(data: dict) -> list[tuple[str, dict]]:
    """Пункты, которых не было в последнем прогоне, но которые мы не выбрасываем."""
    out = []
    goal = data.get("goal")
    if isinstance(goal, dict) and goal.get("missing"):
        out.append(("goal", goal))
    for field in LIST_FIELDS:
        for item in data.get(field) or []:
            if isinstance(item, dict) and item.get("missing"):
                out.append((field, item))
    return out


def is_ready(project: Project) -> bool:
    return bool(getattr(project, "brief_ready", False))


def blocking_questions(data: dict) -> list[dict]:
    """Вопросы, без ответа на которые нельзя начать работу."""
    return [q for q in (data.get("open_questions") or [])
            if isinstance(q, dict) and q.get("blocking")]


def trim_questions(data: dict) -> dict:
    """Не больше BRIEF_MAX_QUESTIONS штук, блокирующие впереди.

    Объединение нескольких прогонов даёт по десятку почти одинаковых
    уточнений; такой список никто не читает.
    """
    questions = [q for q in (data.get("open_questions") or []) if isinstance(q, dict)]
    questions.sort(key=lambda q: (not q.get("blocking"), -int(q.get("seen_count") or 0)))
    data["open_questions"] = questions[:cfg.brief_max_questions]
    return data


async def pending_questions(project: Project) -> list[str]:
    data = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
    if not data:
        return []
    # спрашиваем сперва то, что держит работу
    ordered = sorted((q for q in (data.get("open_questions") or []) if isinstance(q, dict)),
                     key=lambda q: not q.get("blocking"))
    questions = [str(q.get("text")) for q in ordered if q.get("text")]
    if not questions and float(data.get("confidence") or 0) < cfg.brief_min_confidence:
        questions = ["Уточни, пожалуйста, что именно нужно сделать — из переписки "
                     "пока не складывается однозначная картина."]
    return questions[:cfg.brief_questions_per_message]


def question_cooldown() -> dt.timedelta:
    return dt.timedelta(hours=cfg.brief_question_cooldown_h)


class BriefRunner:
    """Периодический сбор ТЗ: пересобрать бриф и, если чего-то не хватает,
    задать клиенту вопросы. Отдельная петля, потому что это работа по
    появлению сообщений, а не по слотам планировщика."""

    def __init__(self, brief: Brief, communicator=None, interval: float = 60.0):
        self.brief = brief
        self.communicator = communicator or brief.communicator
        self.interval = interval

    async def loop(self) -> None:
        import asyncio
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("brief tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self) -> int:
        from .db import spent_today
        if await spent_today() >= cfg.daily_budget_usd:
            log.debug("бюджет исчерпан — бриф не пересобираю")
            return 0

        async with Session() as s:
            projects = (await s.execute(
                select(Project).where(
                    Project.status.notin_(("done", "blocked"))))).scalars().all()

        touched = 0
        for project in projects:
            data = await self.brief.build(project)
            if data is None:
                continue
            touched += 1
            if self.communicator is None:
                continue
            questions = await pending_questions(project)
            if questions:
                await self.communicator.ask_questions(project, questions)
        return touched


# ---------- сверка с эталонным списком требований ----------

COVERAGE_SYSTEM = """Ты проверяешь полноту технического задания.

Тебе дают список ТРЕБОВАНИЙ, написанных человеком в свободной форме, и
список ПУНКТОВ собранного ТЗ. Для каждого требования реши, покрыто ли оно
хотя бы одним пунктом.

Сверяй по смыслу, а не по словам: «оплата картой» и «онлайн-оплата через
эквайринг» — одно и то же. Но «каталог» и «каталог с фильтрами по бренду» —
разное, если требование говорит именно про фильтры.

Вердикты:
  covered   — требование явно покрыто, укажи пункт
  missing   — в ТЗ этого нет
  partial   — покрыто частично или формулировка спорная, объясни чем именно

Отвечай СТРОГО одним JSON-объектом без markdown:
{"results": [{"requirement": "<текст требования как дан>",
              "verdict": "covered|missing|partial",
              "matched": "<пункт ТЗ или пусто>",
              "note": "<коротко, почему>"}]}"""


def brief_lines(data: dict) -> list[str]:
    """Плоский список пунктов брифа для сверки."""
    lines = []
    goal = data.get("goal")
    if isinstance(goal, dict) and goal.get("text"):
        lines.append(f"[цель] {goal['text']}")
    for field in LIST_FIELDS:
        for item in data.get(field) or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or f"{item.get('kind')}: {item.get('name')}"
            mark = " (ОТСУТСТВОВАЛ В ПОСЛЕДНЕМ ПРОГОНЕ)" if item.get("missing") else ""
            prio = f" [{item['priority']}]" if item.get("priority") else ""
            lines.append(f"[{field}]{prio} {text}{mark}")
    return lines


def parse_requirements(text: str) -> list[str]:
    """Эталон пишется руками: одно требование в строке, `#` — комментарий."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


async def check_coverage(requirements: list[str], data: dict, client=None,
                         model: str | None = None) -> list[dict]:
    """Покрыто ли каждое требование эталона хотя бы одним пунктом брифа.

    Отдельный дешёвый вызов: сверять свободный текст построчно бессмысленно,
    а модель это делает по смыслу. Возвращает список вердиктов.
    """
    if not requirements:
        return []
    if client is None:
        key = anthropic_key()
        if not key:
            raise BriefFailed(missing_secret_message("ANTHROPIC_API_KEY"))
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=key)

    prompt = ("# ТРЕБОВАНИЯ\n" + "\n".join(f"- {r}" for r in requirements)
              + "\n\n# ПУНКТЫ ТЗ\n" + "\n".join(f"- {line}" for line in brief_lines(data)))
    assert_no_secrets(prompt)

    # Сверка идёт ПОСЛЕ того, как бриф уже собран и оплачен. Уронить прогон
    # из-за обрыва связи на дешёвом контрольном вызове — обиднее всего,
    # поэтому пробуем несколько раз и в крайнем случае честно говорим,
    # что сверка не состоялась.
    import asyncio
    resp = None
    last_error = None
    for attempt in range(1, 4):
        try:
            resp = await client.messages.create(
                model=model or cfg.coverage_model, max_tokens=2000,
                system=COVERAGE_SYSTEM, messages=[{"role": "user", "content": prompt}])
            break
        except Exception as e:
            last_error = e
            log.warning("сверка покрытия, попытка %s не удалась: %s", attempt, e)
            if attempt < 3:
                await asyncio.sleep(2 * attempt)
    if resp is None:
        log.error("сверка покрытия не состоялась: %s", last_error)
        return [{"requirement": r, "verdict": "partial",
                 "note": f"сверка не выполнена: {last_error}"} for r in requirements]

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        log.warning("сверка покрытия вернула не-JSON: %r", raw[:300])
        return [{"requirement": r, "verdict": "partial",
                 "note": "сверка не разобралась"} for r in requirements]

    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list):
        return [{"requirement": r, "verdict": "partial", "note": "нет поля results"}
                for r in requirements]

    usage = getattr(resp, "usage", None)
    if usage is not None:
        log.info("сверка покрытия: вход %s, выход %s токенов",
                 getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))
    return [r for r in results if isinstance(r, dict)]
