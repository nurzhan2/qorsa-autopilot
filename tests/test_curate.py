"""Правки ТЗ руками.

Главное свойство, которое здесь проверяется: ручной пункт живёт по правилам
`curated` из brief.py — не требует evidence, не помечается `missing`
и переживает полный пересбор. Если он их потеряет, правка владельца молча
исчезнет на следующем прогоне модели, и заметить это будет нечем.
"""
from __future__ import annotations

import pytest

from autopilot import curate
from autopilot.brief import curated_items, is_curated, restore_curated
from autopilot.db import Project, Session
from conftest import make_project


async def brief_of(project_id: int) -> dict:
    async with Session() as s:
        p = await s.get(Project, project_id)
    return (p.brief or {}).get("brief") or {}


@pytest.fixture
async def project(db):
    p = await make_project(title="магазин")
    async with Session() as s:
        row = await s.get(Project, p.id)
        row.brief = {"brief": {
            "goal": {"text": "магазин", "evidence": []},
            "deliverables": [
                {"text": "Корзина", "evidence": ["tg:1:5"], "priority": "must"},
                {"text": "Оплата картой", "evidence": ["tg:1:7"], "priority": "must"},
            ],
            "out_of_scope": [],
        }, "_meta": {}}
        await s.commit()
    return p


async def test_added_requirement_is_curated_and_needs_no_evidence(project):
    """Правило про evidence существует против выдумок МОДЕЛИ.

    Владелец вправе зафиксировать то, чего клиент не говорил, — иначе
    архитектурное решение в ТЗ не запишешь.
    """
    await curate.add(project.id, "Экспорт заказов в CSV")
    items = (await brief_of(project.id))["deliverables"]
    added = next(i for i in items if i["text"] == "Экспорт заказов в CSV")
    assert is_curated(added)
    assert added["origin"] == "owner"
    assert added["evidence"] == []
    assert added["priority"] == "must"


async def test_priority_is_kept(project):
    await curate.add(project.id, "Тёмная тема", priority="nice")
    items = (await brief_of(project.id))["deliverables"]
    assert next(i for i in items if i["text"] == "Тёмная тема")["priority"] == "nice"


async def test_similar_requirement_is_not_duplicated(project):
    """«Корзина» и «Корзина для оформления заказа» — один пункт.

    Тем же нечётким сравнением, что склеивает пункты брифа: иначе каждая
    ручная правка заводила бы соседний пункт про то же самое.
    """
    before = len((await brief_of(project.id))["deliverables"])
    await curate.add(project.id, "Корзина для оформления заказа")
    items = (await brief_of(project.id))["deliverables"]
    assert len(items) == before
    matched = next(i for i in items if "Корзина" in i["text"])
    assert is_curated(matched), "существующий пункт должен стать ручным"


async def test_dropped_requirement_goes_to_out_of_scope(project):
    """Снятое требование не удаляется молча.

    В брифе не должно быть дыр, по которым через месяц не понять,
    требование забыли или отменили осознанно.
    """
    await curate.drop(project.id, "Оплата картой")
    brief = await brief_of(project.id)
    assert all(i["text"] != "Оплата картой" for i in brief["deliverables"])
    gone = next(i for i in brief["out_of_scope"] if i["text"] == "Оплата картой")
    assert gone["origin"] == "owner" and gone["reason"] == curate.OWNER_REASON


async def test_dropped_requirement_can_come_back(project):
    await curate.drop(project.id, "Оплата картой")
    await curate.restore(project.id, "Оплата картой")
    brief = await brief_of(project.id)
    assert any(i["text"] == "Оплата картой" for i in brief["deliverables"])
    assert all(i["text"] != "Оплата картой" for i in brief["out_of_scope"])


async def test_dropping_what_is_not_there_is_an_error(project):
    with pytest.raises(ValueError, match="не найден"):
        await curate.drop(project.id, "Криптокошелёк")


async def test_empty_requirement_refused(project):
    with pytest.raises(ValueError):
        await curate.add(project.id, "   ")


async def test_unknown_field_refused(project):
    with pytest.raises(ValueError, match="поле"):
        await curate.add(project.id, "что-то", field="выдуманное")


async def test_manual_requirement_survives_a_full_rebuild(project):
    """Полный пересбор начинает с чистого листа — ручной пункт обязан вернуться.

    Модель про него ничего не знает и восстановить его не может.
    """
    await curate.add(project.id, "Экспорт заказов в CSV")
    brief = await brief_of(project.id)

    manual = curated_items(brief)
    rebuilt = {"deliverables": [{"text": "Корзина", "evidence": ["tg:1:5"],
                                 "priority": "must"}], "out_of_scope": []}
    restored = restore_curated(rebuilt, manual)

    assert any(i["text"] == "Экспорт заказов в CSV"
               for i in restored["deliverables"])


async def test_listing_shows_what_the_window_needs(project):
    await curate.add(project.id, "Экспорт заказов в CSV")
    await curate.drop(project.id, "Оплата картой")
    async with Session() as s:
        row = await s.get(Project, project.id)
    out = curate.listing(row)
    texts = [r["text"] for r in out["deliverables"]]
    assert "Экспорт заказов в CSV" in texts
    assert "Оплата картой" not in texts
    assert [r["text"] for r in out["out_of_scope"]] == ["Оплата картой"]
    assert next(r for r in out["deliverables"]
                if r["text"] == "Экспорт заказов в CSV")["curated"] is True


async def test_project_without_brief_still_accepts_a_requirement(db):
    """Заказ, по которому ТЗ ещё не собирали, тоже можно дополнить руками."""
    p = await make_project(title="новый")
    await curate.add(p.id, "Первое требование")
    assert (await brief_of(p.id))["deliverables"][0]["text"] == "Первое требование"
