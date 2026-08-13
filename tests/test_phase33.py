"""Фаза 3.3: бриф накапливает, а не перезаписывает."""
from __future__ import annotations

import json

from conftest import make_project
from sqlalchemy import select
from test_brief import CHAT, FakeAnthropic, _Resp, add_msg, ev, reply

from autopilot import roles
from autopilot.brief import (ORIGIN_CONFIRMED, Brief, accumulate, check_coverage,
                             merge_samples, missing_items, parse_requirements)
from autopilot.config import cfg
from autopilot.db import AccessItem, ChatMessage, Project, Session
from autopilot.vault import Vault, ref


async def _reload(p: Project) -> Project:
    async with Session() as s:
        return await s.get(Project, p.id)


def texts(data: dict, field: str = "deliverables") -> list[str]:
    return [d["text"] for d in data.get(field) or []]


def by_text(data: dict, field: str = "deliverables") -> dict[str, dict]:
    return {d["text"]: d for d in data.get(field) or []}


def deliv(text: str, ids=("1",), priority="must", **extra) -> dict:
    item = {"text": text, "evidence": ev(*ids), "priority": priority}
    item.update(extra)
    return item


# ---------- накопление ----------

async def test_brief_never_loses_items(db):
    """Пункт из первого прогона остаётся после второго, где модели его не было."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен каталог с фильтрами", roles.CLIENT)
    await add_msg(p.id, "2", "и корзина", roles.CLIENT)

    first = FakeAnthropic(reply(deliverables=[
        deliv("каталог с фильтрами", ("1",)), deliv("корзина", ("2",))]))
    data = await Brief(client=first).build(p)
    assert sorted(texts(data)) == ["каталог с фильтрами", "корзина"]

    # второй прогон: модель «забыла» корзину
    await add_msg(p.id, "3", "ещё вопрос", roles.CLIENT)
    second = FakeAnthropic(reply(deliverables=[deliv("каталог с фильтрами", ("1",))]))
    data = await Brief(client=second).build(await _reload(p))

    items = by_text(data)
    assert "корзина" in items, "пункт молча исчез из брифа"
    assert items["корзина"]["missing"] is True
    assert items["каталог с фильтрами"].get("missing") is None
    assert items["каталог с фильтрами"]["seen_count"] == 2
    assert items["корзина"]["seen_count"] == 1
    assert items["корзина"]["first_seen"] <= items["корзина"]["last_seen"]

    # и он виден отдельным списком
    assert [t for _, i in missing_items(data) for t in [i["text"]]] == ["корзина"]

    # третий прогон вернул пункт — пометка снимается
    await add_msg(p.id, "4", "и ещё", roles.CLIENT)
    third = FakeAnthropic(reply(deliverables=[
        deliv("каталог с фильтрами", ("1",)), deliv("корзина", ("2",))]))
    data = await Brief(client=third).build(await _reload(p))
    assert by_text(data)["корзина"].get("missing") is None
    assert by_text(data)["корзина"]["seen_count"] == 2


async def test_explicit_rejection_removes(db):
    """Отказ клиента — единственный автоматический путь удаления."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен каталог", roles.CLIENT)
    first = FakeAnthropic(reply(deliverables=[deliv("программа лояльности", ("1",))]))
    data = await Brief(client=first).build(p)
    assert texts(data) == ["программа лояльности"]

    # владелец предложил, клиент отказался
    await add_msg(p.id, "2", "давайте сделаем программу лояльности", roles.OWNER)
    await add_msg(p.id, "3", "нет, не надо", roles.CLIENT)

    second = FakeAnthropic(reply(deliverables=[
        deliv("программа лояльности", ("2", "3"))]))
    data = await Brief(client=second).build(await _reload(p))

    assert texts(data) == [], "отвергнутый пункт не должен оставаться требованием"
    out = texts(data, "out_of_scope")
    assert "программа лояльности" in out
    assert not any(i.get("missing") for i in data["deliverables"])


async def test_priority_extracted(db):
    """«желательно» даёт should или nice, «обязательно» — must."""
    p = await make_project()
    await add_msg(p.id, "1", "обязательно каталог с фильтрами", roles.CLIENT)
    await add_msg(p.id, "2", "Kaspi ещё желательно", roles.CLIENT)

    fake = FakeAnthropic(reply(deliverables=[
        deliv("каталог с фильтрами", ("1",), "must", priority_reason="«обязательно»"),
        deliv("оплата через Kaspi", ("2",), "should", priority_reason="«ещё желательно»"),
    ]))
    data = await Brief(client=fake).build(p)

    items = by_text(data)
    assert items["каталог с фильтрами"]["priority"] == "must"
    assert items["оплата через Kaspi"]["priority"] in ("should", "nice")
    assert "желательно" in items["оплата через Kaspi"]["priority_reason"]


async def test_priority_required_by_schema(db):
    """Пункт без модальности схему не проходит — модель получает повтор."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен каталог", roles.CLIENT)

    bad = json.dumps({
        "goal": {"text": "магазин", "evidence": ev("1")},
        "deliverables": [{"text": "каталог", "evidence": ev("1")}],   # без priority
        "stack": [], "constraints": [], "assets": [], "access_needed": [],
        "open_questions": [], "out_of_scope": [], "confidence": 0.9, "unreadable": [],
    }, ensure_ascii=False)
    good = reply(deliverables=[deliv("каталог", ("1",))])

    fake = FakeAnthropic(bad, good)
    data = await Brief(client=fake).build(p)

    assert len(fake.prompts) == 2, "невалидная модальность должна вызывать повтор"
    assert "priority" in fake.prompts[1]
    assert texts(data) == ["каталог"]


# ---------- доступы ----------

async def test_access_already_received(db, tmp_path, monkeypatch):
    """Доступ, перехваченный в чате, создаётся со статусом received."""
    v = Vault(path=tmp_path / "s.enc", key=__import__("cryptography.fernet",
                                                      fromlist=["Fernet"]).Fernet.generate_key())
    v.set("P1_LABELLED_1", "Str0ngPass123")

    p = await make_project()
    await add_msg(p.id, "1", "хостинг на beget", roles.CLIENT)
    # так выглядит сообщение после перехватчика секретов
    await add_msg(p.id, "2", f"панель: cp.beget.com пароль {ref('P1_LABELLED_1')}",
                  roles.CLIENT)

    fake = FakeAnthropic(reply(access_needed=[
        {"kind": "hosting_panel", "name": "Панель Beget", "evidence": ev("2")},
        {"kind": "domain", "name": "Домен", "evidence": ev("1")},
    ]))
    await Brief(client=fake, vault=v).build(p)

    async with Session() as s:
        rows = {i.name: i for i in (await s.execute(select(AccessItem))).scalars()}

    assert rows["Панель Beget"].status == "received", "клиент уже прислал этот доступ"
    assert rows["Панель Beget"].secret_ref == ref("P1_LABELLED_1")
    assert "переписке" in rows["Панель Beget"].note
    assert rows["Домен"].status == "needed", "домен клиент не присылал"


# ---------- несколько прогонов ----------

async def test_samples_merge(db):
    """При BRIEF_SAMPLES=3 пункт из одного прогона попадает в итог как 1/3."""
    p = await make_project()
    await add_msg(p.id, "1", "нужен каталог", roles.CLIENT)
    await add_msg(p.id, "2", "и корзина", roles.CLIENT)

    common = deliv("каталог", ("1",))
    fake = FakeAnthropic(
        reply(deliverables=[common, deliv("корзина", ("2",))]),
        reply(deliverables=[common]),
        reply(deliverables=[common]),
    )
    data = await Brief(client=fake, samples=3).build(p)

    assert len(fake.prompts) == 3, "должно быть три независимых прогона"
    items = by_text(data)
    assert items["каталог"]["samples"] == "3/3"
    assert items["корзина"]["samples"] == "1/3", "редкий пункт всё равно попадает в итог"


def test_merge_samples_takes_min_confidence():
    a = reply_dict(confidence=0.9)
    b = reply_dict(confidence=0.4)
    merged = merge_samples([a, b])
    assert merged["confidence"] == 0.4, "разброс сам по себе повод не спешить"


def reply_dict(**fields) -> dict:
    data = {"goal": None, "deliverables": [], "stack": [], "constraints": [],
            "assets": [], "access_needed": [], "open_questions": [],
            "out_of_scope": [], "confidence": 0.9, "unreadable": []}
    data.update(fields)
    return data


def test_accumulate_keeps_order():
    prev = {"deliverables": [{"text": "каталог"}, {"text": "корзина"}]}
    fresh = {"deliverables": [{"text": "корзина"}, {"text": "оплата"}]}
    out = accumulate(prev, fresh)
    assert [d["text"] for d in out["deliverables"]] == ["каталог", "корзина", "оплата"]
    assert out["deliverables"][0]["missing"] is True


# ---------- покрытие эталона ----------

def test_parse_requirements():
    text = """# комментарий
    - каталог с фильтрами
    * корзина

    оплата картой
    """
    assert parse_requirements(text) == ["каталог с фильтрами", "корзина", "оплата картой"]


async def test_coverage_eval(db):
    """Эталон-список проверяется на покрытие, непокрытое видно."""
    data = reply_dict(deliverables=[
        {"text": "Каталог товаров с фильтрами по бренду"},
        {"text": "Корзина и оформление заказа"}])

    class FakeCoverage:
        def __init__(self):
            self.messages = self
            self.prompts = []

        async def create(self, *, model, max_tokens, system=None, messages, **kw):
            self.prompts.append(messages[0]["content"])
            return _Resp(json.dumps({"results": [
                {"requirement": "каталог с фильтрами", "verdict": "covered",
                 "matched": "Каталог товаров с фильтрами по бренду"},
                {"requirement": "корзина", "verdict": "covered", "matched": "Корзина"},
                {"requirement": "оплата через Kaspi", "verdict": "missing",
                 "note": "в ТЗ ничего про Kaspi"},
            ]}, ensure_ascii=False))

    fake = FakeCoverage()
    results = await check_coverage(
        ["каталог с фильтрами", "корзина", "оплата через Kaspi"], data, client=fake)

    assert [r["verdict"] for r in results] == ["covered", "covered", "missing"]
    missing = [r for r in results if r["verdict"] == "missing"]
    assert missing and missing[0]["requirement"] == "оплата через Kaspi"
    # в промпт сверки уходят и требования, и пункты брифа
    assert "оплата через Kaspi" in fake.prompts[0]
    assert "Каталог товаров с фильтрами по бренду" in fake.prompts[0]


async def test_coverage_survives_bad_json(db):
    """Сломанная сверка не роняет прогон и не притворяется, что всё покрыто."""
    class Broken:
        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            return _Resp("это не json")

    results = await check_coverage(["каталог"], reply_dict(), client=Broken())
    assert results == [{"requirement": "каталог", "verdict": "partial",
                        "note": "сверка не разобралась"}]


# ---------- сопоставление формулировок ----------

def test_same_item_handles_paraphrase():
    """Модель перефразирует каждый прогон. Регрессия: объединение по точному
    тексту раздувало бриф втрое — 22 пункта вместо десяти на живом чате."""
    from autopilot.brief import same_item

    e34 = ["m3", "m4"]
    # переставленные слова
    assert same_item({"text": "Онлайн-оплата банковской картой", "evidence": e34},
                     {"text": "Оплата картой онлайн", "evidence": e34})
    # короткое внутри длинного
    assert same_item({"text": "Корзина"}, {"text": "Корзина для оформления заказа"})
    assert same_item({"text": "WordPress (CMS)"}, {"text": "WordPress"})
    # одна и та же пара «предложение + согласие»
    pair = ["m18", "m19"]
    assert same_item({"text": "Блог не нужен", "evidence": pair, "origin": ORIGIN_CONFIRMED},
                     {"text": "Блог не входит в проект", "evidence": pair,
                      "origin": ORIGIN_CONFIRMED})
    # один вид доступа и одно сообщение — один доступ
    assert same_item({"kind": "analytics", "name": "Метрика: доступ к счётчику",
                      "evidence": ["m14"]},
                     {"kind": "analytics", "name": "Яндекс.Метрика (счётчик или кабинет)",
                      "evidence": ["m14"]})


def test_same_item_keeps_different_things_apart():
    """Склеить лишнее хуже, чем оставить дубль: пункт исчезнет из ТЗ."""
    from autopilot.brief import same_item

    e4 = ["m4"]
    assert not same_item({"text": "Оплата картой", "evidence": e4},
                         {"text": "Интеграция оплаты через Kaspi", "evidence": e4})
    assert not same_item({"text": "Каталог товаров с фильтрами", "evidence": e4},
                         {"text": "Корзина и оформление заказа", "evidence": e4})
    # разный вид доступа при одинаковом названии
    assert not same_item({"kind": "domain", "name": "Доступ", "evidence": ["m9"]},
                         {"kind": "hosting_panel", "name": "Доступ", "evidence": ["m9"]})


async def test_samples_merge_folds_paraphrases(db):
    """Три прогона с разными формулировками дают ОДИН пункт, а не три."""
    p = await make_project()
    await add_msg(p.id, "1", "нужна корзина и оплата картой", roles.CLIENT)

    fake = FakeAnthropic(
        reply(deliverables=[deliv("Корзина", ("1",))]),
        reply(deliverables=[deliv("Корзина для оформления заказа", ("1",))]),
        reply(deliverables=[deliv("Корзина и оформление заказа покупателем", ("1",))]),
    )
    data = await Brief(client=fake, samples=3).build(p)

    assert len(data["deliverables"]) == 1, [d["text"] for d in data["deliverables"]]
    item = data["deliverables"][0]
    assert item["samples"] == "3/3"
    # остаётся самая подробная формулировка
    assert item["text"] == "Корзина и оформление заказа покупателем"


async def test_coverage_survives_network_failure(db):
    """Обрыв связи на сверке не должен обесценивать уже собранный бриф."""
    class Flaky:
        def __init__(self):
            self.messages = self
            self.calls = 0

        async def create(self, **kw):
            self.calls += 1
            raise ConnectionError("сеть отвалилась")

    client = Flaky()
    results = await check_coverage(["каталог"], reply_dict(), client=client)
    assert client.calls == 3, "должны быть повторы, а не один заход"
    assert results[0]["verdict"] == "partial"
    assert "не выполнена" in results[0]["note"]


async def test_access_items_not_duplicated_by_paraphrase(db):
    """Чеклист держит полосу build — засорять его перефразировками нельзя.

    Регрессия: точное сравнение имён плодило по строке доступа на каждый
    прогон, и на живом чате в базе оказалось 24 записи вместо пяти.
    """
    p = await make_project()
    await add_msg(p.id, "1", "хостинг на beget, домен на reg.kz", roles.CLIENT)

    variants = [
        {"kind": "hosting_panel", "name": "Панель Beget (cp.beget.com)", "evidence": ev("1")},
        {"kind": "hosting_panel", "name": "Панель управления Beget (cp.beget.com)",
         "evidence": ev("1")},
        {"kind": "hosting_panel", "name": "Панель управления хостингом Beget (cp.beget.com)",
         "evidence": ev("1")},
    ]
    for variant in variants:
        await add_msg(p.id, f"m{variants.index(variant)}", "ещё сообщение", roles.CLIENT)
        await Brief(client=FakeAnthropic(reply(access_needed=[variant]))).build(await _reload(p))

    async with Session() as s:
        rows = (await s.execute(select(AccessItem))).scalars().all()
    assert len(rows) == 1, [r.name for r in rows]
    assert rows[0].kind == "hosting_panel"


async def test_samples_merge_is_transitive(db):
    """A~B и B~C при A≁C не должны разваливать пункт на два.

    Похожесть нетранзитивна, поэтому сравнивать надо со всей группой,
    а не только с её первым элементом.
    """
    p = await make_project()
    await add_msg(p.id, "1", "нужна админка", roles.CLIENT)

    fake = FakeAnthropic(
        reply(deliverables=[deliv("Самостоятельно добавлять товары", ("1",))]),
        reply(deliverables=[
            deliv("Возможность самостоятельно добавлять и редактировать товары "
                  "через административную панель", ("1",))]),
        reply(deliverables=[
            deliv("Возможность самостоятельно добавлять и управлять товарами "
                  "через административную панель WordPress", ("1",))]),
    )
    data = await Brief(client=fake, samples=3).build(p)
    assert len(data["deliverables"]) == 1, [d["text"] for d in data["deliverables"]]
