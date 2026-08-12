"""Загрузка scripts/import_tg_export.py в тесты.

Скрипт лежит вне пакета и обычным import не берётся. Тестировать его надо
именно как есть: реальный экспорт клиента пойдёт ровно через этот код.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "import_tg_export.py"

_spec = importlib.util.spec_from_file_location("import_tg_export", SCRIPT)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


async def run_import(path: Path, project_id: int | None = None, *,
                     chat_id: str | None = None, dry: bool = False,
                     client_id: str | None = None, owner_id: str | None = None) -> int:
    return await module.run(Path(path), project_id, chat_id, dry, client_id, owner_id)
