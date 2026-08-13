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

from . import accounts as accounts_cfg
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

# Колонка, привязывающая строку к компании. Нужна, когда таблица одна на
# несколько компаний (наш случай). Значения — алиасы из accounts.toml:
# «qorsa», «hustle», «max»/«МАКС». Пустая или незнакомая ячейка НЕ трактуется
# как компания по умолчанию — см. SheetSync.resolve_company.
COMPANY_COLUMN = "Компания"


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
    def __init__(self, accounts=None, account_ids=None, communicator=None,
                 account=None, account_id: int | None = None):
        """Один синк — одна ФИЗИЧЕСКАЯ ТАБЛИЦА, сколько бы компаний в ней ни было.

        Раньше синк заводился на компанию. При одной общей таблице это значило
        бы три поллера, читающих один и тот же лист и пишущих в него по
        очереди: втрое больше запросов к квоте Google (60 в минуту) и три
        независимых мнения о том, что сейчас в таблице. Поэтому синк один,
        а компанию строки определяет колонка «Компания».

        `account` / `account_id` — форма для одной компании, оставлена ради
        уже написанных вызовов и тестов.
        """
        if account is not None and accounts is None:
            accounts, account_ids = [account], {getattr(account, "code", ""): account_id}
        self.accounts = list(accounts or [])
        self.account_ids = dict(account_ids or {})
        self.communicator = communicator
        self._ws_cache = None
        self._lock = asyncio.Lock()
        # про какие ячейки уже спрашивали — чтобы не долбить владельца
        # одним и тем же вопросом каждые 60 секунд
        self._asked: set[str] = set()

    @property
    def primary(self):
        """Компания, чьи реквизиты таблицы используем для подключения."""
        return self.accounts[0] if self.accounts else None

    @property
    def multi(self) -> bool:
        return len(self.accounts) > 1

    async def ws(self):
        """Авторизация переиспользуется: строить клиента каждые 60 секунд —
        лишний раунд-трип к Google на каждый цикл синка."""
        if self._ws_cache is None:
            self._ws_cache = await asyncio.to_thread(
                _client, getattr(self.primary, "sheet_id", None),
                getattr(self.primary, "sheet_tab", None))
        return self._ws_cache

    def resolve_company(self, row: dict):
        """Компания строки. None означает ВОПРОС ВЛАДЕЛЬЦУ, а не «по умолчанию».

        Одна компания на таблицу — вопрос не стоит, строка её. Несколько —
        решает колонка «Компания» через алиасы из accounts.toml.

        Пустая и незнакомая ячейка обрабатываются ОДИНАКОВО, и это осознанно:
        в обоих случаях мы не знаем, чей это заказ, а цена неверной догадки —
        сообщение клиенту одной компании от лица другой. Подставить сюда
        компанию по умолчанию значит превратить забытую ячейку в чужое письмо.
        """
        if not self.multi:
            return self.primary
        # ищем среди ВСЕХ компаний, включая выключенные: строка «max» при
        # неактивной компании MAX — это опознанная строка, а не загадка
        return accounts_cfg.resolve_by_alias(row.get(COMPANY_COLUMN), self.accounts)

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

        unresolved: list[tuple[int, str, str]] = []   # (строка, клиент, ячейка)

        # фаза 2: только БД, без сетевых вызовов внутри сессии
        async with Session() as s:
            for i, row in enumerate(rows, start=2):
                company = self.resolve_company(row)
                if company is None:
                    # Не знаем, чей это заказ. Пустую строку пропускаем молча —
                    # она просто ещё не заполнена; заполненную выносим владельцу
                    if str(row.get("Клиент", "")).strip() or _num(row.get("ID"), int):
                        unresolved.append((i, str(row.get("Клиент", "")).strip(),
                                           str(row.get(COMPANY_COLUMN, "") or "").strip()))
                    continue

                account_id = self.account_ids.get(company.code)
                if account_id is None:
                    log.error("компания %r есть в accounts.toml, но не заведена в БД — "
                              "строка %s пропущена", company.code, i)
                    continue

                pid = _num(row.get("ID"), int)
                proj = await s.get(Project, pid) if pid else None
                if proj is not None and proj.account_id != account_id:
                    # Проект уже принадлежит другой компании. Перетащить его
                    # молча — это увести заказ между юрлицами по опечатке
                    # в колонке ID или «Компания». Не трогаем и говорим вслух
                    log.error("строка %s: проект %s принадлежит другой компании, "
                              "а в колонке «%s» стоит %r — строка пропущена, "
                              "проект не тронут",
                              i, pid, COMPANY_COLUMN, row.get(COMPANY_COLUMN))
                    unresolved.append((i, str(row.get("Клиент", "")).strip(),
                                       f"{row.get(COMPANY_COLUMN)} (конфликт с проектом {pid})"))
                    continue
                if proj is None:
                    if not str(row.get("Клиент", "")).strip():
                        continue
                    proj = Project(account_id=account_id)
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
                await _link_numeric_chat(s, proj, company.transport)
            await s.commit()

        await self._ask_about_unresolved(unresolved)

        # фаза 3: единственная запись в твою колонку — ID у новых строк,
        # без него бот не свяжет строку с проектом
        if writes:
            await asyncio.to_thread(
                ws.batch_update,
                [{"range": gspread.utils.rowcol_to_a1(r, c), "values": [[v]]} for r, c, v in writes])

    async def _ask_about_unresolved(self, rows: list[tuple[int, str, str]]) -> None:
        """Строки, чью компанию не опознали, — вопросом владельцу, одним списком.

        Не по сообщению на строку: заполнил человек десяток заказов, не глядя
        на колонку, — и получил бы десять уведомлений. И не молча: строка,
        которую синк тихо пропускает, выглядит как «бот не работает», а на
        самом деле её просто некому отдать.

        Повторно про ту же строку не спрашиваем — синк крутится раз в минуту.
        """
        fresh = [r for r in rows if f"{r[0]}:{r[2]}" not in self._asked]
        if not fresh:
            return
        for row in fresh:
            self._asked.add(f"{row[0]}:{row[2]}")

        known = ", ".join(sorted({a.title for a in self.accounts})) or "—"
        lines = [f"  строка {i}: «{client or 'без клиента'}» — в колонке "
                 f"«{COMPANY_COLUMN}» {('стоит ' + repr(cell)) if cell else 'пусто'}"
                 for i, client, cell in fresh]
        text = (f"Не понял, чьи это заказы, и поэтому не трогал их:\n"
                + "\n".join(lines)
                + f"\n\nДопустимые значения: {known}.\n"
                  f"Угадывать не буду: ошибка тут — это сообщение клиенту "
                  f"одной компании от лица другой.")
        log.error("строк с неопознанной компанией: %s (владельцу отправлен список)",
                  len(fresh))
        notify = getattr(self.communicator, "notify_owner", None)
        if notify is not None:
            await notify(text)

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
            mine = [i for i in (self.account_ids.get(a.code) for a in self.accounts)
                    if i is not None]
            if mine:
                # Фильтр по компаниям ЭТОЙ таблицы. Без него синк разложил бы
                # проекты чужой таблицы по своим строкам: sheet_row нумеруется
                # внутри листа, и строка 7 у Qorsa и у Hustle — разные заказы
                q = q.where(Project.account_id.in_(mine))
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


async def _link_numeric_chat(s, proj: Project, default_transport: str = "telegram") -> None:
    """Если менеджер вписал не @username, а готовый chat_id — привязываем сразу.
    Для @username привязка случится по первому входящему сообщению.

    `default_transport` — основной мессенджер КОМПАНИИ проекта: без него
    заказ компании MAX получил бы telegram-канал по умолчанию.
    """
    parsed = parse_chat_ref(proj.chat_ref or "", default_transport)
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
