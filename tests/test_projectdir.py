"""Каталог проекта: одно место на заказ.

Тут проверяется не столько раскладка папок, сколько два свойства, ошибка
в которых стоит потерянной работы: каталог находится по НОМЕРУ проекта,
а не по названию, и явно заданный человеком путь не подменяется нашим.
"""
from __future__ import annotations

import pytest

from autopilot import projectdir
from autopilot.config import cfg
from conftest import make_project


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "projects_dir", tmp_path / "projects")


def test_slug_transliterates_instead_of_dropping():
    """«Интернет-магазин» не должен превращаться в пустоту.

    Выбрасывать не-ASCII было бы проще, но тогда все кириллические названия
    дают одинаковый пустой слаг, и папки заказов различаются только номером.
    """
    assert projectdir.slug("Интернет-магазин") == "internet-magazin"
    assert projectdir.slug("TUR TUK") == "tur-tuk"
    assert projectdir.slug("  Сайт   для  клиента!  ") == "sayt-dlya-klienta"
    assert projectdir.slug("!!!") == ""
    assert len(projectdir.slug("о" * 200)) <= projectdir.MAX_SLUG


async def test_folder_is_created_with_all_subdirs(db):
    p = await make_project(title="TUR TUK")
    folder = projectdir.ensure(p)
    assert folder.name == f"p{p.id}-tur-tuk"
    for sub in projectdir.SUBDIRS:
        assert (folder / sub).is_dir()
    assert (folder / "README.md").exists()


async def test_ensure_is_idempotent(db):
    p = await make_project(title="TUR TUK")
    assert projectdir.ensure(p) == projectdir.ensure(p)
    assert len(list(cfg.projects_dir.iterdir())) == 1


async def test_renamed_project_keeps_its_folder(db):
    """Переименование не заводит вторую папку под тем же номером.

    Иначе половина работы осталась бы в старой, а агент писал бы в новую —
    и заметить это можно было бы только по пустому репозиторию.
    """
    p = await make_project(title="старое название")
    first = projectdir.ensure(p)
    (first / "workspace" / "main.py").write_text("# работа", encoding="utf-8")

    p.title = "совсем другое название"
    second = projectdir.ensure(p)

    assert second == first
    assert (second / "workspace" / "main.py").read_text(encoding="utf-8") == "# работа"


async def test_explicit_workspace_wins(db, tmp_path):
    """Путь из колонки «Папка» ставит человек — мы его не подменяем."""
    mine = tmp_path / "где-то-своё"
    p = await make_project(title="TUR TUK")
    p.workspace = str(mine)
    assert projectdir.workspace(p) == mine
    assert mine.is_dir()


async def test_workspace_defaults_into_project_folder(db):
    p = await make_project(title="TUR TUK")
    p.workspace = None
    assert projectdir.workspace(p) == projectdir.ensure(p) / "workspace"


async def test_find_returns_none_before_anything_created(db):
    p = await make_project(title="TUR TUK")
    assert projectdir.find(p.id) is None
    projectdir.ensure(p)
    assert projectdir.find(p.id) is not None


async def test_snapshot_is_written_readable(db):
    p = await make_project(title="TUR TUK")
    path = projectdir.save_snapshot(p, "brief.json", {"цель": "магазин"})
    assert path.parent.name == "docs"
    assert "магазин" in path.read_text(encoding="utf-8")
