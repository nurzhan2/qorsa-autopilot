"""Разделение колонок: бот пишет только в свои."""
from __future__ import annotations

import gspread
from conftest import make_project, make_tasks, make_account

from autopilot import sheets
from autopilot.db import Project, Session

# Настоящая раскладка рабочей таблицы, порядок колонок как в листе «Задачи».
# Колонки человека и бота и так чередуются — специально путать не нужно.
# Две пары стоят рядом намеренно: «Статус» (твой) против «Стадия заказа»
# (бота) и «Дата выполнения» (твоя) против «Срок» (читается ботом).
HEADER = ["Лист CRM", "Строка CRM", "Дата", "Заказ", "Клиент", "Остаток клиента",
          "Описание", "Приоритет", "Статус", "Дата выполнения", "Стадия заказа",
          "Комментарий", "Нужно от клиента", "Срок", "ID", "Компания",
          "Чат клиента", "Готов к работе", "Папка", "Прогресс", "Превью"]

HUMAN_COLS = {HEADER.index(n) + 1 for n in sheets.HUMAN if n in HEADER}
BOT_COLS = {HEADER.index(n) + 1 for n in sheets.BOT if n in HEADER}


class FakeWorksheet:
    """Google API не дёргается: только то, чем пользуется SheetSync."""

    def __init__(self, records=None):
        self.records = records or []
        self.batches: list[dict] = []

    def row_values(self, n):
        assert n == 1
        return list(HEADER)

    def get_all_records(self):
        return [dict(r) for r in self.records]

    def batch_update(self, data):
        self.batches.extend(data)


def written_cols(ws: FakeWorksheet) -> set[int]:
    return {gspread.utils.a1_to_rowcol(item["range"])[1] for item in ws.batches}


async def test_sheets_column_ownership(db, monkeypatch):
    """push не пишет ни в одну колонку из словаря HUMAN."""
    p = await make_project(title="сайт")
    await make_tasks(p.id, 3)
    async with Session() as s:
        proj = await s.get(Project, p.id)
        proj.sheet_row = 2
        proj.last_action = "собрал главную"
        proj.cost_usd = 1.234
        await s.commit()

    ws = FakeWorksheet()
    monkeypatch.setattr(sheets, "_client", lambda *a, **k: ws)

    sync = sheets.SheetSync()
    await sync._push()

    assert ws.batches, "push вообще ничего не записал — тест был бы пустым"
    cols = written_cols(ws)
    assert cols <= BOT_COLS, f"push залез в чужие колонки: {sorted(cols - BOT_COLS)}"
    assert not (cols & HUMAN_COLS), f"push перезаписал колонки человека: {sorted(cols & HUMAN_COLS)}"
    assert all(gspread.utils.a1_to_rowcol(i["range"])[0] == 2 for i in ws.batches)


async def test_push_writes_progress_and_status(db, monkeypatch):
    p = await make_project(title="сайт", status="active")
    ids = await make_tasks(p.id, 4)
    async with Session() as s:
        proj = await s.get(Project, p.id)
        proj.sheet_row = 5
        t = await s.get(sheets.Task, ids[0])
        t.status = "done"
        await s.commit()

    ws = FakeWorksheet()
    monkeypatch.setattr(sheets, "_client", lambda *a, **k: ws)
    await sheets.SheetSync()._push()

    by_col = {gspread.utils.a1_to_rowcol(i["range"])[1]: i["values"][0][0] for i in ws.batches}
    assert by_col[HEADER.index("Прогресс") + 1] == "1/4"
    # своё состояние бот пишет в «Стадия заказа»...
    assert by_col[HEADER.index("Стадия заказа") + 1] == "в работе"
    # ...а колонку «Статус» не трогает вовсе: она твоя
    assert HEADER.index("Статус") + 1 not in by_col


async def test_pull_touches_only_id_column(db, monkeypatch):
    """Единственная запись бота в твои колонки — проставленный ID новой строки."""
    base = {c: "" for c in HEADER}
    ws = FakeWorksheet(records=[{
        **base,
        "Клиент": "Айгерим", "Заказ": "лендинг", "Чат клиента": "42",
        "Срок": "01.09.2026", "Приоритет": "1",
        # чекбокс приходит СТРОКОЙ «TRUE», а не bool — проверено на живой таблице
        "Готов к работе": "TRUE",
        # твой статус заполнен: бот обязан его не тронуть
        "Статус": "В работе", "Дата выполнения": "04.08.2026",
    }])
    monkeypatch.setattr(sheets, "_client", lambda *a, **k: ws)

    acc = await make_account()
    await sheets.SheetSync(account=acc, account_id=acc.id)._pull()

    assert written_cols(ws) == {HEADER.index("ID") + 1}

    async with Session() as s:
        from sqlalchemy import select
        proj = (await s.execute(select(Project))).scalars().one()
    assert proj.client == "Айгерим"
    assert proj.title == "лендинг", "заголовок берётся из колонки «Заказ»"
    assert proj.priority == 1
    assert proj.deadline.isoformat() == "2026-09-01", "срок читается из «Срок»"
    assert proj.status == "briefing"       # появился чат -> new -> briefing
    assert proj.ready_for_work is True
    assert proj.sheet_row == 2
    assert proj.account_id == acc.id, "новый проект остался без компании"
