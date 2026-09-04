"""Слепок рабочего каталога: что агент реально тронул за сессию.

Главное свойство, которое здесь проверяется, — не «нашёл изменения»,
а **честность про неполноту**: пустой список при неснятом слепке нельзя
читать как «нарушений нет». Именно так выглядит молчаливый пропуск,
которого этот проект избегает везде.
"""
from __future__ import annotations

import os

from autopilot import worktrace


def write(root, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_created_modified_deleted_are_separated(tmp_path):
    """Создать файл и переписать его — РАЗНЫЕ вещи, и путать их нельзя:
    на этой разнице стоит `evidence` (написать тест — работа, переписать
    существующий — потеря доказательства)."""
    write(tmp_path, "keep.py")
    write(tmp_path, "old.py")
    before = worktrace.snapshot(tmp_path)

    write(tmp_path, "new.py")
    write(tmp_path, "keep.py", "изменено — и размер тоже другой")
    os.remove(tmp_path / "old.py")

    changed = worktrace.diff(before, worktrace.snapshot(tmp_path))
    assert changed.created == ("new.py",)
    assert changed.modified == ("keep.py",)
    assert changed.deleted == ("old.py",)
    assert changed.touched == ("keep.py", "old.py")     # без созданного
    assert set(changed.paths) == {"new.py", "keep.py", "old.py"}


def test_untouched_tree_yields_nothing(tmp_path):
    write(tmp_path, "a/b.py")
    before = worktrace.snapshot(tmp_path)
    changed = worktrace.diff(before, worktrace.snapshot(tmp_path))
    assert not changed.paths and changed.ok


def test_heavy_dirs_are_skipped(tmp_path):
    """Без исключений слепок проекта 8 — 6654 файла вместо 333, и каждая
    задача выглядела бы как правка тысяч файлов."""
    before = worktrace.snapshot(tmp_path)
    write(tmp_path, "node_modules/pkg/index.js")
    write(tmp_path, ".venv/Lib/site-packages/x.py")
    write(tmp_path, ".git/objects/ab/cdef")
    write(tmp_path, "backend/app/main.py")

    changed = worktrace.diff(before, worktrace.snapshot(tmp_path))
    assert changed.created == ("backend/app/main.py",)


def test_unreadable_tree_is_not_reported_as_clean(tmp_path, monkeypatch):
    """`os.walk` без `onerror` пропускает нечитаемое МОЛЧА, и слепок вернул бы
    ok=True — «нарушений нет» там, где на самом деле «не смотрели»."""
    write(tmp_path, "a.py")

    def broken_walk(*args, **kw):
        on_error = kw.get("onerror")
        if on_error is not None:
            on_error(PermissionError(13, "нет доступа", str(tmp_path / "secret")))
        return iter(())

    monkeypatch.setattr(worktrace.os, "walk", broken_walk)
    snap = worktrace.snapshot(tmp_path)
    assert snap.ok is False
    assert "не прочитано" in snap.why


def test_missing_before_is_not_all_created(tmp_path):
    """Нет слепка «до» — сравнивать не с чем, и всё после сессии выглядело бы
    созданным ею. Такой ответ хуже отсутствия ответа."""
    write(tmp_path, "a.py")
    empty = worktrace.Snapshot({}, ok=False, why="слепок «до» не снят")
    changed = worktrace.diff(empty, worktrace.snapshot(tmp_path))
    assert changed.ok is False
    assert not changed.paths


def test_missing_directory_is_honest(tmp_path):
    snap = worktrace.snapshot(tmp_path / "нет-такого")
    assert snap.ok is False and "каталога нет" in snap.why


def test_around_gives_the_difference(tmp_path):
    write(tmp_path, "before.py")
    with worktrace.around(tmp_path) as trace:
        write(tmp_path, "after.py")
    assert trace.changed.created == ("after.py",)


def test_around_does_not_swallow_exceptions(tmp_path):
    """Наблюдатель не вправе глотать исключение того, за кем наблюдает:
    провал сессии агента должен дойти до планировщика."""
    try:
        with worktrace.around(tmp_path):
            raise RuntimeError("сессия упала")
    except RuntimeError as e:
        assert "сессия упала" in str(e)
    else:
        raise AssertionError("исключение проглочено")
