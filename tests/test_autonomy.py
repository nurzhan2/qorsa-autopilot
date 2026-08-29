"""Режим самостоятельности: работа с заглушками, ключи в конце.

Главное, что здесь проверяется, — не то, что режим снимает гейт, а то, что
он НЕ ослабляет приёмку. Проверка, которой нужен настоящий ключ, не должна
ни выполняться, ни считаться пройденной, ни выглядеть дефектом работы.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from autopilot import autonomy
from autopilot.db import AccessItem, Project, Session, Task, utcnow
from autopilot.vault import Vault
from autopilot.verifier import Verdict
from conftest import make_access, make_project, make_tasks


@pytest.fixture
def v(tmp_path):
    return Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())


# ---------- имена секретов ----------

def test_secret_name_is_transliterated_and_scoped():
    item = AccessItem(id=7, project_id=8, name="Платёжный шлюз", kind="api_key")
    assert autonomy.suggest_secret_name(8, item) == "P8_API_KEY_PLATEZHNYY_SHLYUZ_7"


def test_two_similar_items_never_share_a_secret_name():
    """Поймано на живых данных проекта 8.

    «Платёжная система (карты + СБП в рублях)» и «Платёжная система (карты +
    СБП для рублёвых платежей русскими картами)» — разные пункты чеклиста,
    а читаемый хвост имени у них обрезался до одного и того же. Вставленное
    значение одного молча затёрло бы значение другого.
    """
    a = AccessItem(id=28, project_id=8, kind="api_key",
                   name="Платёжная система (карты + СБП в рублях) — шлюз не определён")
    b = AccessItem(id=30, project_id=8, kind="api_key",
                   name="Платёжная система (карты + СБП для рублёвых платежей)")
    assert autonomy.suggest_secret_name(8, a) != autonomy.suggest_secret_name(8, b)


def test_item_without_a_readable_name_still_gets_one():
    item = AccessItem(id=3, project_id=8, name="!!!", kind="other")
    assert autonomy.suggest_secret_name(8, item) == "P8_OTHER_3"


def test_existing_secret_ref_is_never_regenerated():
    """Под старым именем ключ мог уже разойтись по коду проекта."""
    item = AccessItem(id=7, project_id=8, name="Платёжный шлюз", kind="api_key",
                      secret_ref="{{SECRET:YOOKASSA_SECRET}}")
    assert autonomy.suggest_secret_name(8, item) == "YOOKASSA_SECRET"


# ---------- включение ----------

async def test_enable_fills_placeholders_and_lifts_the_gate(db, v):
    p = await make_project(status="blocked_access")
    await make_access(p.id, name="Платёжный шлюз", kind="api_key")
    await make_access(p.id, name="Карты", kind="api_key")

    out = await autonomy.enable(p.id, vault=v)

    assert len(out["placeholders"]) == 2
    async with Session() as s:
        items = (await s.execute(
            select(AccessItem).where(AccessItem.project_id == p.id))).scalars().all()
        project = await s.get(Project, p.id)
    assert {i.status for i in items} == {autonomy.PLACEHOLDER}
    assert project.autonomous_mode is True
    for name in out["placeholders"]:
        assert autonomy.is_placeholder(v.get(name))


async def test_enable_never_overwrites_a_real_key(db, v):
    """Настоящий ключ заглушкой не подменяется ни при каких обстоятельствах."""
    p = await make_project()
    item = await make_access(p.id, name="Хостинг", kind="hosting_panel",
                             status="verified")
    async with Session() as s:
        row = await s.get(AccessItem, item.id)
        row.secret_ref = "{{SECRET:HOSTING}}"
        await s.commit()
    v.set("HOSTING", "настоящий-пароль")

    await autonomy.enable(p.id, vault=v)

    assert v.get("HOSTING") == "настоящий-пароль"
    async with Session() as s:
        assert (await s.get(AccessItem, item.id)).status == "verified"


async def test_disable_returns_items_to_waiting(db, v):
    p = await make_project()
    await make_access(p.id, name="Карты", kind="api_key")
    await autonomy.enable(p.id, vault=v)

    await autonomy.disable(p.id)

    async with Session() as s:
        item = (await s.execute(
            select(AccessItem).where(AccessItem.project_id == p.id))).scalars().first()
        project = await s.get(Project, p.id)
    assert item.status == "needed"
    assert project.autonomous_mode is False


# ---------- гейт полосы build ----------

async def test_gate_ignores_placeholders_but_not_real_gaps(db, v):
    from autopilot.scheduler import Scheduler

    with_placeholder = await make_project(title="с заглушкой")
    still_waiting = await make_project(title="ждёт по-настоящему")
    await make_access(with_placeholder.id, name="Карты", kind="api_key")
    await make_access(still_waiting.id, name="Карты", kind="api_key")
    await autonomy.enable(with_placeholder.id, vault=v)

    blocked = await Scheduler(None, None, None)._projects_without_access()

    assert with_placeholder.id not in blocked
    assert still_waiting.id in blocked


async def test_requirement_that_left_the_brief_stops_blocking(db):
    """Пункт, пропавший из ТЗ, работу не держит.

    Поймано на живых данных проекта 8: шесть пунктов из восьми оказались
    переформулировками, не склеившимися при пересборе брифа. Помеченные
    `stale`, они продолжали держать полосу build — и вдобавок по ним шли
    напоминания клиенту, то есть мы просили доступ к тому, чего в ТЗ нет.
    """
    from autopilot.scheduler import Scheduler

    p = await make_project()
    item = await make_access(p.id, name="Старый дубль", kind="api_key")
    async with Session() as s:
        row = await s.get(AccessItem, item.id)
        row.stale = True
        await s.commit()

    assert p.id not in await Scheduler(None, None, None)._projects_without_access()


# ---------- приёмка ----------

def test_check_referencing_a_placeholder_is_deferred():
    blocked = {"YOOKASSA_SECRET"}
    check = {"type": "shell", "cmd": "curl -H \"Authorization: $YOOKASSA_SECRET\" ..."}
    assert autonomy.deferred_reason(check, blocked)


def test_check_without_the_key_runs_as_usual():
    check = {"type": "shell", "cmd": "cd backend && .venv/Scripts/python -m pytest"}
    assert autonomy.deferred_reason(check, {"YOOKASSA_SECRET"}) is None


def test_no_placeholders_means_nothing_is_deferred():
    check = {"type": "shell", "cmd": "echo $YOOKASSA_SECRET"}
    assert autonomy.deferred_reason(check, set()) is None


def test_placeholder_marker_is_found_in_output():
    msg = "shell: exit=1 — invalid key PLACEHOLDER::P8_API_KEY_KARTY"
    assert autonomy.output_hit_placeholder(msg)
    assert autonomy.placeholders_in_output(msg) == ["P8_API_KEY_KARTY"]


def test_deferred_verdict_never_counts_as_accepted():
    """Самое важное свойство режима.

    Задача, часть проверок которой не выполнялась, принятой быть не может —
    иначе режим превращается в способ закрывать задачи, ничего не проверив.
    """
    verdict = Verdict(ok=True, defects=[], deterministic=2,
                      deferred=("shell: нужен настоящий ключ",),
                      deferred_keys=("YOOKASSA_SECRET",))
    assert verdict.confirmed is False
    assert verdict.unproven is False
    assert verdict.awaiting_keys is True


def test_verdict_without_deferred_behaves_as_before():
    assert Verdict(ok=True, defects=[], deterministic=1).confirmed is True
    assert Verdict(ok=True, defects=[], deterministic=0).unproven is True
    assert Verdict(ok=True, defects=[], deterministic=1).awaiting_keys is False


async def test_verifier_does_not_execute_a_check_that_needs_a_real_key(db, v, monkeypatch):
    """Проверка не выполняется, а не выполняется-и-трактуется.

    Живой запрос к платёжному шлюзу с фальшивым ключом ничего не проверяет,
    а его ответ пришлось бы разбирать гаданием.
    """
    import autopilot.autonomy as autonomy_mod
    import autopilot.verifier as verifier_mod

    p = await make_project()
    item = await make_access(p.id, name="Карты", kind="api_key")
    await autonomy.enable(p.id, vault=v)
    monkeypatch.setattr(autonomy_mod, "default_vault", v)
    secret = autonomy.suggest_secret_name(p.id, item)

    ran = []

    # Подменяем МЕТОД, а не элемент `handlers`: это property, и запись
    # в него уходит во временный словарь — проверка при этом идёт настоящая
    async def spy(self, check, cwd, task, project):
        ran.append(check)
        return True, ""

    monkeypatch.setattr(verifier_mod.Verifier, "_check_shell", spy)
    verifier = verifier_mod.Verifier()

    async with Session() as s:
        project = await s.get(Project, p.id)
    task = Task(project_id=p.id, title="Интеграция карт", acceptance=[
        {"type": "shell", "cmd": f"python pay.py --key ${secret}"},
        {"type": "shell", "cmd": "cd backend && .venv/Scripts/python -m pytest"},
    ])
    async with Session() as s:
        s.add(task)
        await s.commit()
        await s.refresh(task)

    verdict = await verifier.run(task, project)

    assert len(ran) == 1                       # выполнилась только вторая
    assert verdict.deferred and verdict.awaiting_keys
    assert verdict.confirmed is False


# ---------- настоящий ключ ----------

async def test_put_key_verifies_the_item_and_stores_the_value(db, v):
    p = await make_project()
    item = await make_access(p.id, name="Карты", kind="api_key")
    await autonomy.enable(p.id, vault=v)

    out = await autonomy.put_key(p.id, item.id, "живой-ключ-42", vault=v)

    assert v.get(out["secret"]) == "живой-ключ-42"
    async with Session() as s:
        row = await s.get(AccessItem, item.id)
    assert row.status == "verified" and row.verified_at is not None


async def test_put_key_refuses_a_placeholder_as_the_value(db, v):
    p = await make_project()
    item = await make_access(p.id, name="Карты", kind="api_key")
    await autonomy.enable(p.id, vault=v)
    with pytest.raises(ValueError, match="заглушка"):
        await autonomy.put_key(p.id, item.id,
                               autonomy.make_placeholder("X"), vault=v)


async def test_put_key_refuses_empty(db, v):
    p = await make_project()
    item = await make_access(p.id, name="Карты", kind="api_key")
    with pytest.raises(ValueError):
        await autonomy.put_key(p.id, item.id, "   ", vault=v)


async def test_only_tasks_waiting_for_that_key_come_back(db, v):
    """У проекта не хватает трёх ключей — первый вставленный не должен
    отправлять на переприёмку задачи, которые провалятся на двух остальных."""
    p = await make_project()
    item = await make_access(p.id, name="Карты", kind="api_key")
    await autonomy.enable(p.id, vault=v)
    name = autonomy.suggest_secret_name(p.id, item)

    mine, other = await make_tasks(p.id, 2, lane="verify", status="ready")
    async with Session() as s:
        a = await s.get(Task, mine)
        a.status = autonomy.AWAITING_KEYS
        a.last_error = "ждёт настоящих ключей: {{SECRET:%s}}" % name
        b = await s.get(Task, other)
        b.status = autonomy.AWAITING_KEYS
        b.last_error = "ждёт настоящих ключей: {{SECRET:СОВСЕМ_ДРУГОЙ}}"
        await s.commit()

    out = await autonomy.put_key(p.id, item.id, "живой", vault=v)

    assert out["resumed"] == 1
    async with Session() as s:
        assert (await s.get(Task, mine)).status == "ready"
        assert (await s.get(Task, mine)).lane == "verify"
        assert (await s.get(Task, other)).status == autonomy.AWAITING_KEYS


async def test_scheduler_does_not_burn_an_attempt_on_missing_keys(db, v):
    """Попытка не засчитывается — по той же причине, что при упоре в квоту:
    пересобирать нечего, агент ничего не чинил бы."""
    from autopilot.scheduler import Scheduler

    p = await make_project()
    (task_id,) = await make_tasks(p.id, 1, lane="verify")
    async with Session() as s:
        project = await s.get(Project, p.id)
        task = await s.get(Task, task_id)

    verdict = Verdict(ok=True, defects=[], deterministic=0,
                      deferred=("shell: нужен настоящий ключ",),
                      deferred_keys=("YOOKASSA_SECRET",))
    await Scheduler(None, None, None)._awaiting_keys(task, project, verdict)

    async with Session() as s:
        row = await s.get(Task, task_id)
    assert row.status == autonomy.AWAITING_KEYS
    assert row.attempts == 0
    assert "YOOKASSA_SECRET" in row.last_error
    assert autonomy.waiting_for(row) == ["YOOKASSA_SECRET"]
