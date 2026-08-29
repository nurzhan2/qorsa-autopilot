"""Приём заказа без Google-таблицы: выгрузку чата вставили в окно.

Таблица — не единственный способ завести заказ, и до появления живого ingest
она была ещё и неудобным: чтобы автопилот увидел проект, надо было завести
строку, проставить чат, дождаться синка. Здесь короче — человек кладёт в окно
выгрузку переписки, и из неё сразу получается проект с историей, по которой
собирается ТЗ.

Что этот модуль ОБЯЗАН делать так же, как живой ingest, — иначе вставленный
заказ поедет по другим правилам, чем принятый ботом:

* **перехват секретов до записи в базу.** Пароль из старой переписки не
  окажется в `chat_messages` открытым — уедет в vault, в тексте останется
  `{{SECRET:NAME}}`;
* **роли у каждой реплики.** ТЗ строится по словам КЛИЕНТА; реплика владельца
  требованием не становится, пока клиент с ней не согласился;
* **дедупликация.** Ту же выгрузку вставят второй раз — новых сообщений
  не появится.

Форматы, которые понимает `parse()`:

1. `telegram_json` — `result.json` из Telegram Desktop, тот же, что у
   `scripts/import_tg_export.py`;
2. `dialog` — переписка текстом: `Имя: реплика`, либо шапка
   `Имя, [дата]` с текстом на следующих строках (так копирует Telegram);
3. `plain` — просто текст ТЗ без ролей. Всё считается словами клиента.

Формат `dialog` опознаётся только когда в тексте ДВА и более собеседника,
каждый минимум с двумя репликами. Иначе обычное ТЗ со строкой «Цель: сделать
сайт» разбиралось бы как реплика собеседника по имени «Цель».
"""
from __future__ import annotations

import dataclasses as dc
import datetime as dt
import hashlib
import json
import logging
import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import accounts as accounts_cfg
from . import projectdir, roles
from .db import (Account, ChatMessage, Project, ProjectChat, Session, utcnow)
from .secrets_scan import scrub

log = logging.getLogger("intake")

# Отдельное имя транспорта, а не «telegram»: у этого чата нет живого канала,
# и притворяться телеграмом значит однажды попробовать в него ответить.
# Транспорта с таким именем не поднято, поэтому исходящие честно уходят
# в заглушку и никуда не отправляются — см. Communicator._deliver.
TRANSPORT = "paste"

TG_JSON, DIALOG, PLAIN = "telegram_json", "dialog", "plain"

# Как в выгрузке может называться владелец. Список нужен только для ПОДСКАЗКИ
# в окне: роль всё равно подтверждает человек перед тем, как заводить проект.
OWNER_HINTS = ("я", "i", "me", "owner", "qorsa", "hustle", "админ", "manager",
               "менеджер", "владелец")

# Шапка вида «Анастасия, [12.08.2026 14:30]» — так копирует Telegram Desktop.
RE_HEAD_COMMA = re.compile(r"^(?P<who>[^\n:\[\]]{1,48}),\s*\[(?P<when>[^\]]{4,40})\]\s*$")
# «[12.08.2026 14:30] Анастасия: привет»
RE_HEAD_BRACKET = re.compile(
    r"^\[(?P<when>[^\]]{4,40})\]\s*(?P<who>[^\n:]{1,48}):\s*(?P<text>.*)$")
# «Анастасия: привет»
RE_HEAD_COLON = re.compile(r"^(?P<who>[^\n:]{1,48}):\s*(?P<text>.*)$")

# Сколько слов может быть в имени собеседника. «Цель проекта — сделать так,
# чтобы» именем не является, и без этого предела любая строка с двоеточием
# объявлялась бы репликой.
MAX_SPEAKER_WORDS = 4


@dc.dataclass
class Line:
    """Одна реплика из выгрузки, ещё без роли и без перехвата секретов."""
    idx: int
    speaker: str
    text: str
    at: dt.datetime | None = None
    sender_id: str | None = None
    has_media: bool = False
    media_kind: str | None = None
    reply_to: str | None = None
    raw: dict = dc.field(default_factory=dict)


@dc.dataclass
class Parsed:
    kind: str
    lines: list[Line]
    # кто говорил и сколько раз — окно показывает это, чтобы человек расставил роли
    speakers: dict[str, int]
    notes: list[str] = dc.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.lines)


# ---------- разбор ----------

def _speaker_ok(who: str) -> bool:
    who = who.strip()
    if not who or len(who.split()) > MAX_SPEAKER_WORDS:
        return False
    # «Итого» и «Примечание» — не собеседники; настоящее имя не заканчивается
    # знаком препинания и не содержит переносов
    return not who.endswith((".", "!", "?", ",", ";"))


def _parse_dialog(raw: str) -> Parsed:
    """Переписка текстом. Строки без шапки принадлежат предыдущему говорящему."""
    lines: list[Line] = []
    speakers: dict[str, int] = {}
    current: Line | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current.text.strip():
            current.text = current.text.strip()
            lines.append(current)
            speakers[current.speaker] = speakers.get(current.speaker, 0) + 1
        current = None

    for row in (raw or "").splitlines():
        stripped = row.strip()
        head = None
        for pattern in (RE_HEAD_COMMA, RE_HEAD_BRACKET, RE_HEAD_COLON):
            m = pattern.match(stripped)
            if m and _speaker_ok(m.group("who")):
                head = m
                break
        if head is not None:
            flush()
            body = head.groupdict().get("text") or ""
            current = Line(idx=len(lines), speaker=head.group("who").strip(),
                           text=body, at=_parse_when(head.groupdict().get("when")))
            continue
        if current is None:
            # текст до первой шапки: ничей, но терять его нельзя
            current = Line(idx=0, speaker="", text="")
        current.text += ("\n" if current.text else "") + row
    flush()

    for i, line in enumerate(lines):
        line.idx = i
    return Parsed(kind=DIALOG, lines=lines, speakers=speakers)


def _parse_when(value: str | None) -> dt.datetime | None:
    """Дата из шапки. Не разобралась — не беда: порядок реплик и так известен,
    а выдуманная дата хуже отсутствующей."""
    if not value:
        return None
    text = value.strip().replace("/", ".")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _looks_like_dialog(parsed: Parsed) -> bool:
    """Два и более собеседника, у каждого хотя бы по две реплики.

    Порог не придирка: обычное ТЗ полно строк «Цель:», «Сроки:», «Бюджет:»,
    и по одному вхождению каждой они выглядят как реплики восьми разных людей.
    """
    named = {k: v for k, v in parsed.speakers.items() if k}
    return len(named) >= 2 and sum(1 for v in named.values() if v >= 2) >= 2


def _tg_lines(data: dict) -> Parsed:
    """result.json из Telegram Desktop — тот же формат, что у import_tg_export."""
    lines: list[Line] = []
    speakers: dict[str, int] = {}
    notes: list[str] = []
    service = broken = 0

    for msg in (data.get("messages") or []):
        if not isinstance(msg, dict):
            broken += 1
            continue
        if str(msg.get("type") or "") != "message":
            service += 1          # вступления, закрепления, звонки
            continue
        text = _tg_text(msg.get("text"))
        if not text.strip():
            text = _tg_poll(msg)
        has_media, kind = _tg_media(msg)
        if not text.strip() and not has_media:
            broken += 1
            continue
        who = str(msg.get("from") or "").strip() or _tg_sender(msg) or "?"
        lines.append(Line(
            idx=len(lines), speaker=who, text=text,
            at=_tg_date(msg), sender_id=_tg_sender(msg),
            has_media=has_media, media_kind=kind,
            reply_to=(str(msg["reply_to_message_id"])
                      if msg.get("reply_to_message_id") else None),
            raw={"id": msg.get("id"), "date": msg.get("date"),
                 "forwarded_from": msg.get("forwarded_from")},
        ))
        speakers[who] = speakers.get(who, 0) + 1

    if service:
        notes.append(f"сервисных записей пропущено: {service}")
    if broken:
        notes.append(f"не разобрано и пропущено: {broken}")
    return Parsed(kind=TG_JSON, lines=lines, speakers=speakers, notes=notes)


def _tg_text(value) -> str:
    """`text` бывает строкой, а бывает списком кусков с разметкой."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for piece in value:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict):
                parts.append(str(piece.get("text", "")))
        return "".join(parts)
    return "" if value is None else str(value)


def _tg_poll(msg: dict) -> str:
    poll = msg.get("poll")
    if not isinstance(poll, dict):
        return ""
    question = str(poll.get("question") or "").strip()
    return f"[опрос] {question}" if question else "[опрос]"


_MEDIA_TYPES = {"voice_message": "voice", "video_message": "video_note",
                "sticker": "sticker", "animation": "animation",
                "video_file": "video", "audio_file": "audio"}
_MEDIA_HINTS = (("photo", "photo"), ("file", "document"),
                ("sticker_emoji", "sticker"), ("location_information", "location"),
                ("contact_information", "contact"), ("poll", "poll"))


def _tg_media(msg: dict) -> tuple[bool, str | None]:
    raw_type = msg.get("media_type")
    if raw_type:
        return True, _MEDIA_TYPES.get(str(raw_type), str(raw_type))
    for key, kind in _MEDIA_HINTS:
        if msg.get(key):
            return True, kind
    return False, None


def _tg_sender(msg: dict) -> str | None:
    raw = str(msg.get("from_id") or "")
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits or raw


def _tg_date(msg: dict) -> dt.datetime | None:
    unix = msg.get("date_unixtime")
    if unix:
        try:
            return dt.datetime.fromtimestamp(int(unix), dt.timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    raw = msg.get("date")
    if raw:
        try:
            return dt.datetime.fromisoformat(str(raw)).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return None


def parse(raw: str) -> Parsed:
    """Что нам вставили. Формат определяется содержимым, а не выбором в окне:
    ошибиться в выпадающем списке легко, а последствие — потерянная переписка."""
    text = (raw or "").strip()
    if not text:
        return Parsed(kind=PLAIN, lines=[], speakers={},
                      notes=["пусто — вставлять нечего"])

    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return _tg_lines(data)
        if isinstance(data, list):
            return _tg_lines({"messages": data})

    dialog = _parse_dialog(text)
    if _looks_like_dialog(dialog):
        return dialog

    return Parsed(kind=PLAIN, speakers={"": 1},
                  lines=[Line(idx=0, speaker="", text=text)],
                  notes=["ролей в тексте не видно — всё засчитано как слова клиента"])


def guess_roles(parsed: Parsed, account=None) -> dict[str, str]:
    """Подсказка «кто здесь клиент»: собеседник, похожий на меня, — владелец.

    Именно ПОДСКАЗКА. Ошибка в роли портит ТЗ целиком (мои же предложения
    уедут в требования клиента), поэтому окно показывает раскладку и просит
    подтвердить, а не расставляет роли молча.
    """
    signature = str(getattr(account, "signature", "") or
                    getattr(account, "name", "") or "").lower()
    out: dict[str, str] = {}
    for who in parsed.speakers:
        low = who.strip().lower()
        owner = low in OWNER_HINTS or (signature and signature in low) or \
            any(low.startswith(h) for h in OWNER_HINTS if len(h) > 2)
        out[who] = roles.OWNER if owner else roles.CLIENT
    return out


# ---------- запись ----------

def _message_id(line: Line) -> str:
    """Идентификатор реплики, выведенный из её содержимого.

    Так повторная вставка той же выгрузки не заводит вторую копию: уникальный
    индекс (transport, chat_id, tg_message_id) отсечёт её, как отсекает дубли
    живого потока. Позиция входит в хеш намеренно — два честных «ок» подряд
    остаются двумя сообщениями.
    """
    if line.raw.get("id"):
        return str(line.raw["id"])
    blob = f"{line.idx}|{line.speaker}|{line.text}".encode("utf-8")
    return "h" + hashlib.sha1(blob).hexdigest()[:16]


def chat_id_for(project_id: int) -> str:
    return f"paste:p{project_id}"


async def apply(project_id: int, parsed: Parsed, role_map: dict[str, str],
                *, store: bool = True, source: str = "вставлено вручную") -> dict:
    """Записать разобранные реплики в переписку проекта.

    `source` — КОРОТКАЯ ПОМЕТКА о происхождении («вставлено в дашборде»),
    а не сама выгрузка: она уходит в `raw_json` каждой реплики, и класть туда
    весь текст значит размножить его по сотне строк — вместе с тем, что
    перехватчик в них уже вырезал.

    `store=False` — пробный прогон: не трогает ни базу, ни vault. Ровно та же
    оговорка, что у импорта: прогон, который «ничего не пишет», но складывает
    секреты в хранилище, состояние всё-таки меняет.
    """
    chat = chat_id_for(project_id)
    account = await _account_of(project_id)

    async with Session() as s:
        project = await s.get(Project, int(project_id))
    if project is None:
        raise ValueError(f"проекта {project_id} нет")

    # Реплики без даты выстраиваем в ряд перед «сейчас»: порядок в брифе
    # считается по created_at, и одинаковое время у всех сломало бы его
    base = utcnow() - dt.timedelta(seconds=len(parsed.lines) + 1)

    added = skipped = secrets_found = 0
    by_role: dict[str, int] = {}
    clean: list[tuple[str, str, str]] = []      # (роль, кто, вычищенный текст)

    for line in parsed.lines:
        role = role_map.get(line.speaker) or roles.CLIENT
        by_role[role] = by_role.get(role, 0) + 1

        text, names = scrub(line.text, project_id=project_id, chat_id=chat,
                            store=store)
        secrets_found += len(names)
        clean.append((role, line.speaker, text))
        if not store:
            added += 1
            continue

        sender = line.sender_id or (f"paste:{line.speaker}" if line.speaker else None)
        if sender:
            await roles.remember(account, TRANSPORT, chat, sender,
                                 line.speaker, role=role)

        row = ChatMessage(
            transport=TRANSPORT, chat_id=chat,
            tg_message_id=_message_id(line), project_id=project_id,
            direction="in" if role == roles.CLIENT else "out",
            sender_id=sender, sender_role=role, text=text,
            has_media=line.has_media, media_kind=line.media_kind,
            reply_to=line.reply_to,
            # ПОМЕТКА о происхождении, а не сама выгрузка. В первой версии сюда
            # уходил весь вставленный текст — то есть плоская копия переписки
            # с непойманными секретами в каждой из ста строк разом
            raw_json={"pasted": True, "source": source, "speaker": line.speaker,
                      **{k: v for k, v in line.raw.items() if v is not None}},
            created_at=line.at or (base + dt.timedelta(seconds=line.idx)),
        )
        async with Session() as s:
            s.add(row)
            try:
                await s.commit()
                added += 1
            except IntegrityError:
                await s.rollback()
                skipped += 1        # эту реплику уже вставляли — так и должно быть

    if store:
        await _ensure_chat_row(project_id, chat)
        _save_transcript(project, parsed, clean)

    return {"added": added, "skipped": skipped, "secrets": secrets_found,
            "roles": by_role, "kind": parsed.kind, "notes": list(parsed.notes)}


async def _ensure_chat_row(project_id: int, chat: str) -> None:
    async with Session() as s:
        found = (await s.execute(
            select(ProjectChat).where(ProjectChat.transport == TRANSPORT,
                                      ProjectChat.chat_id == chat))).scalars().first()
        if found is None:
            s.add(ProjectChat(project_id=project_id, transport=TRANSPORT,
                              chat_id=chat, handle="вставлено вручную",
                              is_primary=True, is_group=False))
            await s.commit()


def _save_transcript(project, parsed: Parsed,
                     clean: list[tuple[str, str, str]]) -> None:
    """Копия принятой переписки в intake/ проекта — ПОСЛЕ перехвата секретов.

    Здесь лежит соблазн сохранить то, что вставили, — оригинал ведь удобнее,
    если разбор окажется кривым. Так и было сделано в первой версии, и на
    первом же живом прогоне через дашборд пароль из переписки лёг в файл
    открытым: в базу он не попал, а на диск попал. Перехват, который
    обходится записью рядом, — не перехват.

    Поэтому пишем разобранное и вычищенное: структура для глаз сохранена,
    значений нет. Оригинал у владельца и так остаётся — в самом мессенджере.
    """
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    path = projectdir.intake(project) / f"{stamp}-{parsed.kind}.txt"
    head = [
        f"# разобранная копия выгрузки, формат: {parsed.kind}",
        f"# реплик: {len(clean)}, принято: {utcnow().isoformat()[:19]} UTC",
        "# значения паролей и ключей вырезаны при приёме — они в хранилище,",
        "# здесь на их месте ссылки {{SECRET:ИМЯ}}",
        "",
    ]
    body = [f"[{role}] {who or '—'}: {text}" for role, who, text in clean]
    try:
        path.write_text("\n".join(head + body), encoding="utf-8")
    except OSError as e:
        log.warning("копия выгрузки не сохранена: %s", e)


async def _account_of(project_id: int):
    from .db import Project as P
    code = accounts_cfg.DEFAULT_CODE
    async with Session() as s:
        proj = await s.get(P, int(project_id))
        if proj is not None:
            row = await s.get(Account, proj.account_id)
            if row is not None:
                code = row.code
    return accounts_cfg.by_code(code) or accounts_cfg.load()[0]


# ---------- заведение проекта ----------

async def create_project(*, account_code: str, client: str, title: str,
                         deadline: dt.date | None = None,
                         priority: int = 2) -> Project:
    """Проект без строки в таблице.

    `ready_for_work` ставится сразу: галочка в таблице существует, чтобы
    менеджер подтвердил заказ, а здесь заказ заводит сам владелец — второго
    подтверждения от того же человека спрашивать незачем. Два остальных гейта
    полосы build (доступы и собранный бриф) на месте и не ослаблены.
    """
    async with Session() as s:
        row = (await s.execute(
            select(Account).where(Account.code == str(account_code)))).scalars().first()
        if row is None:
            known = (await s.execute(select(Account.code))).scalars().all()
            raise ValueError(f"компании {account_code!r} нет; есть: {sorted(known)}")
        project = Project(
            account_id=row.id, client=(client or "").strip(),
            title=(title or "").strip() or "без названия",
            status="briefing", ready_for_work=True,
            priority=int(priority or 2), deadline=deadline,
        )
        s.add(project)
        await s.commit()
        await s.refresh(project)

    projectdir.ensure(project)
    log.info("заведён проект %s (%s, компания %s) — из вставленной выгрузки",
             project.id, project.title, account_code)
    return project


async def message_count(project_id: int) -> int:
    async with Session() as s:
        return int((await s.execute(
            select(func.count()).select_from(ChatMessage)
            .where(ChatMessage.project_id == project_id))).scalar() or 0)
