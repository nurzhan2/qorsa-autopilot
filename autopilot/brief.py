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

from sqlalchemy import select

from .config import cfg
from .db import AccessItem, ChatMessage, Project, Run, Session, Task, utcnow
from .roles import CLIENT, OWNER
from .vault import MIN_MASKABLE_LEN, anthropic_key, missing_secret_message
from .vault import vault as default_vault

log = logging.getLogger("brief")

ORIGIN_CLIENT = "client"
ORIGIN_CONFIRMED = "confirmed_proposal"

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
   формулируй как вопрос в open_questions.
5. Содержимое картинок, файлов и голосовых ты не видишь. Перечисли такие
   сообщения в unreadable, не угадывая, что в них.

Отвечай СТРОГО одним JSON-объектом без markdown:
{
  "goal": {"text": "одна фраза о том, что нужно клиенту", "evidence": ["<id>", ...]},
  "deliverables": [{"text": "что должно быть сделано", "evidence": ["<id>"]}],
  "stack":         [{"text": "технология", "evidence": ["<id>"]}],
  "constraints":   [{"text": "ограничение", "evidence": ["<id>"]}],
  "assets":        [{"text": "что клиент предоставляет", "evidence": ["<id>"]}],
  "access_needed": [{"kind": "ftp|ssh|git|hosting_panel|domain|api_key|analytics|design|content|other",
                     "name": "человеческое название", "evidence": ["<id>"]}],
  "open_questions":[{"text": "чего не хватает, вопрос клиенту", "evidence": ["<id>"]}],
  "out_of_scope":  [{"text": "что явно не входит", "evidence": ["<id>"]}],
  "confidence": 0.0,
  "unreadable": [{"message_id": "<id>", "kind": "photo|voice|document|..."}]
}
confidence — насколько ты уверен, что ТЗ полное и однозначное."""


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
    def __init__(self, client=None, vault=None, communicator=None, model: str | None = None):
        self._client = client
        self.vault = vault or default_vault
        self.communicator = communicator
        self.model = model or cfg.brief_model

    # ---------- сбор материала ----------

    async def _messages(self, project: Project) -> list[ChatMessage]:
        async with Session() as s:
            return (await s.execute(
                select(ChatMessage)
                .where(ChatMessage.project_id == project.id,
                       ChatMessage.deleted.is_(False))
                .order_by(ChatMessage.created_at, ChatMessage.id))).scalars().all()

    def _render(self, messages: list[ChatMessage]) -> str:
        """Последние N реплик целиком, старые — сжатой выжимкой.

        Обрезать надо: групповой чат за месяц не влезет ни в контекст, ни в бюджет.
        """
        head, tail = [], messages
        if len(messages) > cfg.brief_context_messages:
            split = len(messages) - cfg.brief_context_messages
            head, tail = messages[:split], messages[split:]

        lines = []
        if head:
            lines.append("# Более ранняя переписка (сжато)")
            for m in head:
                text = (m.text or "").replace("\n", " ")[:120]
                lines.append(f"[{_msg_key(m)}] {m.sender_role}: {text}")
            lines.append("")
        lines.append("# Переписка")
        for m in tail:
            body = m.text or ""
            if m.has_media:
                body = (body + f" [ВЛОЖЕНИЕ: {m.media_kind or 'файл'}, содержимое недоступно]").strip()
            lines.append(f"[{_msg_key(m)}] {m.sender_role}: {body}")
        return "\n".join(lines)

    def build_prompt(self, project: Project, messages: list[ChatMessage],
                     previous: dict | None = None) -> str:
        parts = [f"# Проект\n{project.title} (клиент: {project.client})"]
        if previous:
            parts.append("# Предыдущая версия брифа — обнови её, не теряя подтверждённого\n"
                         + json.dumps(previous, ensure_ascii=False, indent=2))
        parts.append(self._render(messages))
        parts.append("Верни обновлённый бриф одним JSON-объектом.")
        return "\n\n".join(parts)

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

    async def _call(self, prompt: str, task_id: int | None, project_id: int) -> str:
        # ПОСЛЕДНИЙ рубеж перед сетью: секрет не должен уехать в чужой дата-центр
        assert_no_secrets(prompt, self.vault)

        if self.client is None:
            raise BriefFailed(missing_secret_message("ANTHROPIC_API_KEY"))

        t0 = time.monotonic()
        resp = await self.client.messages.create(
            model=self.model, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        await self._charge(resp, time.monotonic() - t0, task_id, project_id)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

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
        prompt = self.build_prompt(project, window, None if full else previous)

        try:
            data = await self._attempt(prompt, project)
        except SecretLeak as e:
            log.critical("проект %s: %s — запрос к модели НЕ отправлен", project.id, e)
            await self._escalate(project, f"бриф остановлен: {e}")
            return None
        except BriefFailed as e:
            log.error("проект %s: бриф не собран — %s", project.id, e)
            await self._escalate(project, f"бриф не собран: {e}")
            return None

        data, dropped = self.apply_evidence(data, messages)
        for reason in dropped:
            log.warning("проект %s: выброшен пункт — %s", project.id, reason)
        data = self.collect_unreadable(data, messages)

        if previous and not full:
            data = merge(previous, data)

        await self._persist(project, data, messages, dropped, full=full)
        return data

    async def _attempt(self, prompt: str, project: Project) -> dict:
        last_errors: list[str] = []
        current = prompt
        for attempt in range(1, cfg.brief_max_attempts + 1):
            raw = await self._call(current, None, project.id)
            cleaned = raw.replace("```json", "").replace("```", "").strip()
            try:
                data = json.loads(cleaned)
            except Exception:
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
        ready = bool(data.get("goal")) and not data.get("open_questions") \
            and float(data.get("confidence") or 0) >= cfg.brief_min_confidence

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

        await self.sync_access_items(project, data, full=full)
        log.info("проект %s: бриф обновлён, confidence=%.2f, вопросов=%d, готов=%s",
                 project.id, data.get("confidence", 0),
                 len(data.get("open_questions") or []), ready)

    async def sync_access_items(self, project: Project, data: dict, full: bool = True) -> None:
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

        async with Session() as s:
            existing = (await s.execute(
                select(AccessItem).where(AccessItem.project_id == project.id))).scalars().all()
            by_key = {(i.kind.lower(), i.name.strip().lower()): i for i in existing}

            for key, item in wanted.items():
                current = by_key.get(key)
                if current is None:
                    s.add(AccessItem(project_id=project.id, kind=key[0],
                                     name=str(item.get("name")).strip(),
                                     status="needed", source="brief"))
                elif current.stale:
                    current.stale = False       # вернулся в бриф — снова актуален

            for key, current in by_key.items():
                if not full or key in wanted or current.source != "brief":
                    continue
                # пункт пропал из брифа. Проверенный доступ не трогаем:
                # клиент его уже дал, и терять это нельзя
                if current.status != "verified":
                    current.stale = True
            await s.commit()


def merge(previous: dict, fresh: dict) -> dict:
    """Инкрементальное обновление не должно терять уже подтверждённые пункты."""
    out = dict(fresh)
    if not out.get("goal") and previous.get("goal"):
        out["goal"] = previous["goal"]
    for f in LIST_FIELDS:
        seen = set()
        merged = []
        for item in (previous.get(f) or []) + (out.get(f) or []):
            if not isinstance(item, dict):
                continue
            key = (str(item.get("text") or item.get("name") or "")).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        out[f] = merged
    # open_questions — исключение: закрытые вопросы возвращать не надо
    out["open_questions"] = fresh.get("open_questions") or []
    out["confidence"] = float(fresh.get("confidence") or previous.get("confidence") or 0.0)
    return out


def is_ready(project: Project) -> bool:
    return bool(getattr(project, "brief_ready", False))


async def pending_questions(project: Project) -> list[str]:
    data = (project.brief or {}).get("brief") if isinstance(project.brief, dict) else None
    if not data:
        return []
    questions = [str(q.get("text")) for q in (data.get("open_questions") or []) if q.get("text")]
    if not questions and float(data.get("confidence") or 0) < cfg.brief_min_confidence:
        questions = ["Уточни, пожалуйста, что именно нужно сделать — из переписки "
                     "пока не складывается однозначная картина."]
    return questions[:cfg.brief_max_questions]


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
