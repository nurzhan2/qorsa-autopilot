"""Двусторонний синк с Google-таблицей.

ПРАВИЛО: таблица — витрина, БД — источник правды.
Колонки поделены по владельцу. Бот НИКОГДА не пишет в твои колонки.

Порядок внутри цикла важен: сначала _pull (таблица → БД), потом _push (БД → таблица).
Оба идут строго по очереди под одним локом, параллельно их не запускают —
иначе _push мог бы разложить значения по sheet_row, которые _pull прямо сейчас
переписывает. Внутри каждого из них сетевые вызовы gspread (asyncio.to_thread)
и работа с БД разнесены по фазам: сначала читаем таблицу, потом целиком
отрабатываем в БД, и только затем пишем в таблицу — сессия БД никогда не
держится открытой поверх сетевого вызова.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import case, func, select

from .config import cfg
from .db import AccessItem, Project, ProjectChat, Session, Task, utcnow
from .ingest import parse_chat_ref

log = logging.getLogger("sheets")

# --- твои колонки: бот только читает ---
HUMAN = {
    "ID": "id",
    "Клиент": "client",
    "Проект": "title",
    "Чат клиента": "chat_ref",
    "Цена": "price",
    "Дедлайн": "deadline",
    "Приоритет": "priority",
    "Папка": "workspace",
    "Готов к работе": "ready_for_work",
    # только для режима одной общей таблицы; в раздельном её просто нет.
    # Держим в HUMAN, чтобы бот гарантированно в неё не писал
    "Компания": "account_id",
}
# --- колонки бота: он перезаписывает их батчем ---
BOT = ["Статус", "Прогресс", "Ждём от клиента", "Превью", "Последнее действие",
       "Стоимость $", "Обновлено"]

# Колонка выросла из «TG chat»: старое имя продолжаем читать, чтобы уже
# заполненные таблицы не пришлось править руками
CHAT_COLUMNS = ("Чат клиента", "TG chat")

assert not (set(HUMAN) & set(BOT)), "колонка не может принадлежать и человеку, и боту"

STATUS_RU = {"new": "новый", "briefing": "уточняю ТЗ", "active": "в работе",
             "review": "на проверке", "blocked": "🔴 нужен ты", "done": "сдан",
             "blocked_access": "жду доступы"}

TRUE_WORDS = {"true", "да", "yes", "1", "y", "+", "✓", "✔", "истина", "готов"}

# Колонка для режима «одна таблица на все компании». В раздельном режиме
# (по умолчанию) её не нужно заводить вовсе.
COMPANY_COLUMN = "Компания"
DEFAULT_CODE = "qorsa"


def _flag(v) -> bool:
    """Галочка в Google Sheets приходит как bool, а руками её пишут как угодно."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in TRUE_WORDS


def _client(sheet_id: str | None = None, sheet_tab: str | None = None) -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(
        cfg.google_creds,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return (gspread.authorize(creds)
            .open_by_key(sheet_id or cfg.sheet_id)
            .worksheet(sheet_tab or cfg.sheet_tab))


def _parse_date(v):
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(str(v).strip(), f).date()
        except Exception:
            pass
    return None


def _chat_ref(row) -> str | None:
    """«Чат клиента»: tg:@user / max:@user / @user / числовой id.
    Без префикса — telegram."""
    for name in CHAT_COLUMNS:
        value = str(row.get(name, "") or "").strip()
        if value:
            return value
    return None


def _num(v, cast=float):
    try:
        return cast(str(v).replace(" ", "").replace(",", ".").replace("\xa0", ""))
    except Exception:
        return None


class SheetSync:
    def __init__(self, account=None, account_id: int | None = None,
                 shared: bool = False):
        """Один синк — одна КОМПАНИЯ.

        `shared=True` означает, что этой таблицей пользуются несколько
        компаний: тогда строки различаются колонкой «Компания», и чужие
        синк не трогает. По умолчанию таблицы раздельные, и колонка не нужна.
        """
        self.account = account
        self.account_id = account_id
        self.shared = shared
        self._ws_cache = None
        self._lock = asyncio.Lock()

    @property
    def code(self) -> str:
        return str(getattr(self.account, "code", "") or "")

    async def ws(self):
        """Авторизация переиспользуется: строить клиента каждые 60 секунд —
        лишний раунд-трип к Google на каждый цикл синка."""
        if self._ws_cache is None:
            self._ws_cache = await asyncio.to_thread(
                _client, getattr(self.account, "sheet_id", None),
                getattr(self.account, "sheet_tab", None))
        return self._ws_cache

    def _row_is_mine(self, row: dict) -> bool:
        """Строка общей таблицы принадлежит этой компании?

        В раздельном режиме таблица целиком наша — вопрос не стоит. В общей
        решает колонка «Компания»: пустое значение считаем своим только для
        компании по умолчанию, иначе строку без метки подхватили бы сразу все.
        """
        if not self.shared:
            return True
        mark = str(row.get(COMPANY_COLUMN, "") or "").strip().lower()
        if not mark:
            return self.code == DEFAULT_CODE
        return mark in (self.code.lower(), str(getattr(self.account, "name", "")).lower())

    async def loop(self) -> None:
        while True:
            try:
                async with self._lock:
                    await self._pull()
                    await self._push()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ws_cache = None      # протухший токен/сокет — пересоздадим
                log.exception("sheet sync failed")
            await asyncio.sleep(cfg.sheet_sync_sec)

    # ---------- таблица → БД ----------

    async def _pull(self) -> None:
        ws = await self.ws()
        # фаза 1: читаем таблицу
        rows = await asyncio.to_thread(ws.get_all_records)
        header = await asyncio.to_thread(ws.row_values, 1)

        writes: list[tuple[int, int, str]] = []   # (row, col, value) — проставить ID новым строкам
        id_col = header.index("ID") + 1 if "ID" in header else None

        # фаза 2: только БД, без сетевых вызовов внутри сессии
        async with Session() as s:
            for i, row in enumerate(rows, start=2):
                if not self._row_is_mine(row):
                    continue          # чужая компания в общей таблице
                pid = _num(row.get("ID"), int)
                proj = await s.get(Project, pid) if pid else None
                if proj is not None and self.account_id and proj.account_id != self.account_id:
                    # Проект уже принадлежит другой компании. Перетащить его
                    # сюда молча — это увести заказ между юрлицами по опечатке
                    # в колонке ID. Не трогаем и говорим вслух
                    log.error("строка %s таблицы %s ссылается на проект %s чужой "
                              "компании — пропущена", i, self.code, pid)
                    continue
                if proj is None:
                    if not str(row.get("Клиент", "")).strip():
                        continue
                    proj = Project(account_id=self.account_id)
                    s.add(proj)
                    await s.flush()
                    if id_col:
                        writes.append((i, id_col, str(proj.id)))

                proj.sheet_row = i
                proj.client = str(row.get("Клиент", "")).strip()
                proj.title = str(row.get("Проект", "")).strip()
                proj.chat_ref = _chat_ref(row)
                proj.price = _num(row.get("Цена"))
                proj.deadline = _parse_date(row.get("Дедлайн"))
                proj.priority = _num(row.get("Приоритет"), int) or 2
                proj.workspace = str(row.get("Папка", "")).strip() or proj.workspace
                proj.ready_for_work = _flag(row.get("Готов к работе"))
                if proj.status == "new" and proj.chat_ref:
                    proj.status = "briefing"
                proj.updated_at = utcnow()
                await _link_numeric_chat(s, proj)
            await s.commit()

        # фаза 3: единственная запись в твою колонку — ID у новых строк,
        # без него бот не свяжет строку с проектом
        if writes:
            await asyncio.to_thread(
                ws.batch_update,
                [{"range": gspread.utils.rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in writes])

    # ---------- БД → таблица ----------


    async def _push(self) -> None:
        ws = await self.ws()
        header = await asyncio.to_thread(ws.row_values, 1)
        # жёсткий фильтр: индексы колонок берём ТОЛЬКО из BOT
        cols = {name: header.index(name) + 1 for name in BOT if name in header}
        human_cols = {header.index(name) + 1 for name in HUMAN if name in header}
        if not cols:
            log.warning("в таблице нет колонок бота: %s", BOT)
            return
        overlap = set(cols.values()) & human_cols
        assert not overlap, f"колонки бота пересеклись с твоими: {overlap}"

        async with Session() as s:
            q = select(Project).where(Project.sheet_row.isnot(None))
            if self.account_id:
                # без этого фильтра синк разложил бы проекты одной компании
                # по строкам таблицы другой: sheet_row у них свой на таблицу
                q = q.where(Project.account_id == self.account_id)
            projects = (await s.execute(q)).scalars().all()
            progress = await self._progress_map(s)
            waiting = await self._waiting_map(s)

        updates = []
        for p in projects:
            vals = {
                "Статус": STATUS_RU.get(p.status, p.status),
                "Прогресс": progress.get(p.id, ""),
                # менеджеру видно, почему проект стоит, и он не дёргает владельца
                "Ждём от клиента": waiting.get(p.id, ""),
                "Превью": p.preview_url or "",
                "Последнее действие": p.last_action or "",
                "Стоимость $": round(p.cost_usd, 2),
                # в БД всё в UTC, в таблице человеку нужно местное время
                "Обновлено": p.updated_at.astimezone().strftime("%d.%m %H:%M"),
            }
            for name, col in cols.items():
                updates.append({"range": gspread.utils.rowcol_to_a1(p.sheet_row, col),
                                "values": [[vals[name]]]})

        # батчем — иначе упрёшься в квоту Sheets (60 запросов/мин)
        for chunk in (updates[i:i + 200] for i in range(0, len(updates), 200)):
            await asyncio.to_thread(ws.batch_update, chunk)

    async def _waiting_map(self, s) -> dict[int, str]:
        """Недостающие доступы одной строкой. Только НАЗВАНИЯ пунктов —
        ни значений, ни ссылок на секреты в таблицу не попадает никогда."""
        rows = (await s.execute(
            select(AccessItem.project_id, AccessItem.name)
            .where(AccessItem.status != "verified")
            .order_by(AccessItem.project_id, AccessItem.id))).all()
        out: dict[int, list[str]] = {}
        for pid, name in rows:
            out.setdefault(pid, []).append(name)
        return {pid: ", ".join(names) for pid, names in out.items()}

    async def _progress_map(self, s) -> dict[int, str]:
        """Один запрос на все проекты вместо сессии на каждый."""
        rows = (await s.execute(
            select(Task.project_id,
                   func.count(Task.id),
                   func.sum(case((Task.status == "done", 1), else_=0)))
            .group_by(Task.project_id))).all()
        return {pid: f"{int(done or 0)}/{total}" for pid, total, done in rows}


async def _link_numeric_chat(s, proj: Project) -> None:
    """Если менеджер вписал не @username, а готовый chat_id — привязываем сразу.
    Для @username привязка случится по первому входящему сообщению."""
    parsed = parse_chat_ref(proj.chat_ref or "")
    if not parsed:
        return
    transport, handle = parsed
    if handle.startswith("@") or not handle.lstrip("-").isdigit():
        return
    exists = (await s.execute(
        select(ProjectChat).where(ProjectChat.transport == transport,
                                  ProjectChat.chat_id == handle))).scalars().first()
    if exists is None:
        s.add(ProjectChat(project_id=proj.id, transport=transport,
                          chat_id=handle, is_primary=True))
