"""Проверка, предмет которой правил проверяемый.

Единственное место приёмки, где право закрыть задачу фактически было отдано
тому, кого проверяют: критерий `pytest tests/test_cart.py`, а каталог `tests/`
пишет и держит сам агент в том же каталоге.

Правило узкое намеренно, и половина тестов здесь — про то, ЧЕГО оно не ловит:
ложное пятно стоит решения человека на каждой такой задаче, а соблазн жать
`/confirm` не глядя растёт ровно с потоком подтверждений.
"""
from __future__ import annotations

import pytest

from autopilot import evidence
from autopilot.config import cfg


PYTEST_CART = {"type": "shell",
               "cmd": "cd backend && .venv/Scripts/python -m pytest tests/test_cart.py"}


def test_named_files_are_extracted():
    assert evidence.files_of(PYTEST_CART) == {"tests/test_cart.py"}


def test_node_id_is_stripped():
    check = {"type": "shell", "cmd": "pytest tests/test_orders.py::test_total"}
    assert evidence.files_of(check) == {"tests/test_orders.py"}


def test_directory_is_not_a_file():
    """`pytest tests/` не называет файл. Подставить вместо каталога «всё,
    что похоже на тест» — это уже догадка, а не факт."""
    assert evidence.files_of({"type": "shell", "cmd": "pytest tests/"}) == set()


def test_flags_are_not_files():
    check = {"type": "shell", "cmd": "pytest -q --maxfail=1 tests/test_cart.py"}
    assert evidence.files_of(check) == {"tests/test_cart.py"}


# ---------- само правило ----------

def test_rewriting_the_named_test_spoils_the_check():
    """Ровно тот случай, ради которого модуль написан."""
    assert evidence.tainted(PYTEST_CART, {"backend/tests/test_cart.py"})


def test_creating_the_test_is_work_not_a_stain():
    """Написать тест — это работа, ради которой задачу и заводили.
    В `touched` попадают только `modified` и `deleted`, поэтому созданного
    здесь нет по построению — тест закрепляет, что так и должно быть."""
    assert not evidence.tainted(PYTEST_CART, set())


def test_a_different_file_does_not_spoil_the_check():
    assert not evidence.tainted(PYTEST_CART, {"backend/app/cart.py"})


def test_similar_name_is_not_the_same_file():
    """Совпадение по хвосту требует границы сегмента: `test_cart.py`
    не должен совпадать с `my_test_cart.py`."""
    assert not evidence.tainted(PYTEST_CART, {"backend/tests/my_test_cart.py"})


def test_relative_criterion_matches_absolute_change():
    """Критерий пишется относительно СВОЕГО каталога (`cd backend && ...`),
    а слепок — относительно корня рабочего каталога."""
    assert evidence.tainted({"type": "shell", "cmd": "pytest tests/test_cart.py"},
                            {"backend/tests/test_cart.py"})


def test_review_returns_index_of_the_spoiled_check():
    acceptance = [{"type": "http", "url": "http://localhost:8000/cart", "expect": 401},
                  PYTEST_CART]
    out = evidence.review(acceptance, {"backend/tests/test_cart.py"})
    assert set(out) == {1}                      # только второй критерий
    assert out[1] == {"tests/test_cart.py"}


def test_switch_off_disables_the_rule(monkeypatch):
    """Выключатель обязан выключать: механизм новый, и на живых данных
    он ещё не срабатывал ни разу."""
    monkeypatch.setattr(cfg, "evidence_taint", False)
    assert evidence.review([PYTEST_CART], {"backend/tests/test_cart.py"}) == {}


def test_note_names_the_file():
    text = evidence.note({"tests/test_cart.py"})
    assert "tests/test_cart.py" in text and "не считается" in text


def test_known_blind_spot_content_is_not_read():
    """ГРАНИЦА, а не недосмотр: содержимое правки мы не читаем. Тест,
    поправленный по делу (сменился интерфейс), и тест, ослабленный ради
    зелёного прогона, для этого модуля неотличимы.

    Размен осознанный: ложное пятно стоит одного решения человека,
    пропущенное — закрытой задачи, которую никто не проверял.
    """
    assert evidence.tainted(PYTEST_CART, {"backend/tests/test_cart.py"})
