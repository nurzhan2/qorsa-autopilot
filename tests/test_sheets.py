"""Разделение колонок: бот пишет только в свои."""
from __future__ import annotations

import gspread
from conftest import make_project, make_tasks, make_account

from autopilot import sheets
from autopilot.db import Project, Session

# колонки нарочно вперемешку — чтобы индексы человека и бота чередовались
HEADER = ["ID", "Клиент", "Статус", "Проект", "Прогресс", "TG chat", "Цена",
          "Превью", "Дедлайн", "Приоритет", "Последнее действие", "Папка",
          "Готов к работе", "Ждём от клиента", "Стоимость $", "Обновлено"]

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
    assert by_col[HEADER.index("Статус") + 1] == "в работе"


async def test_pull_touches_only_id_column(db, monkeypatch):
    """Единственная запись бота в твои колонки — проставленный ID новой строки."""
    ws = FakeWorksheet(records=[{
        "ID": "", "Клиент": "Айгерим", "Проект": "лендинг", "TG chat": "42",
        "Цена": "150 000", "Дедлайн": "01.09.2026", "Приоритет": "1", "Папка": "",
        "Готов к работе": "TRUE",
        "Статус": "", "Прогресс": "", "Ждём от клиента": "", "Превью": "",
        "Последнее действие": "", "Стоимость $": "", "Обновлено": "",
    }])
    monkeypatch.setattr(sheets, "_client", lambda *a, **k: ws)

    acc = await make_account()
    await sheets.SheetSync(account=acc, account_id=acc.id)._pull()

    assert written_cols(ws) == {HEADER.index("ID") + 1}

    async with Session() as s:
        from sqlalchemy import select
        proj = (await s.execute(select(Project))).scalars().one()
    assert proj.client == "Айгерим"
    assert proj.price == 150000.0
    assert proj.priority == 1
    assert proj.deadline.isoformat() == "2026-09-01"
    assert proj.status == "briefing"       # появился TG chat -> new -> briefing
    assert proj.ready_for_work is True
    assert proj.sheet_row == 2
    assert proj.account_id == acc.id, "новый проект остался без компании"
