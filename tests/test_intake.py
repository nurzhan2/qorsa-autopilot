"""Приём заказа из вставленной выгрузки чата.

Проверяется ровно то, что отличает этот путь от «просто записать текст
в базу»: перехват секретов, роли у реплик, дедупликация повторной вставки
и то, что обычное ТЗ не разбирается как диалог.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from autopilot import intake, projectdir, roles
from autopilot.config import cfg
from autopilot.db import ChatMessage, ProjectChat, Session
from autopilot.vault import Vault
from conftest import make_account

TG_COPY = """Анастасия, [12.08.2026 14:30]
привет! нужно приложение доставки для туристов

Я, [12.08.2026 14:31]
понял. Кемер или вся Анталия?

Анастасия, [12.08.2026 14:35]
пока Кемер, и курьеру отдельное приложение

Я, [12.08.2026 14:36]
предлагаю Flutter на оба

Анастасия, [12.08.2026 14:40]
да, хорошо"""

COLONS = """Анастасия: нужен интернет-магазин косметики
Я: на каком движке?
Анастасия: на ваше усмотрение
Я: предлагаю WordPress
Анастасия: давайте"""

PLAIN_SPEC = """Цель: лендинг для студии.
Сроки: две недели.
Нужна форма заявки и интеграция с CRM.
Бюджет: обсуждается."""


# ---------- разбор ----------

def test_parses_telegram_copy_with_date_headers():
    p = intake.parse(TG_COPY)
    assert p.kind == intake.DIALOG
    assert len(p.lines) == 5
    assert p.speakers == {"Анастасия": 3, "Я": 2}
    assert p.lines[0].text == "привет! нужно приложение доставки для туристов"
    assert p.lines[0].at is not None and p.lines[0].at.year == 2026


def test_parses_name_colon_dialog():
    p = intake.parse(COLONS)
    assert p.kind == intake.DIALOG
    assert len(p.lines) == 5
    assert p.speakers == {"Анастасия": 3, "Я": 2}


def test_plain_spec_is_not_mistaken_for_dialog():
    """«Цель:», «Сроки:», «Бюджет:» — не собеседники.

    Без порога «двое, у каждого минимум по две реплики» обычное ТЗ
    разбиралось бы как переписка четырёх человек, и ни одна строка не
    досталась бы клиенту целиком.
    """
    p = intake.parse(PLAIN_SPEC)
    assert p.kind == intake.PLAIN
    assert len(p.lines) == 1
    assert "форма заявки" in p.lines[0].text


def test_parses_telegram_json_export():
    data = {"id": 555, "type": "personal_chat", "messages": [
        {"id": 1, "type": "message", "from": "Анастасия", "from_id": "user111",
         "date_unixtime": "1755000000", "text": "нужен магазин"},
        {"id": 2, "type": "service", "action": "pin_message"},
        {"id": 3, "type": "message", "from": "Я", "from_id": "user777",
         "date_unixtime": "1755000060",
         "text": ["смотри ", {"type": "link", "text": "пример.рф"}]},
        {"id": 4, "type": "message", "from": "Анастасия", "from_id": "user111",
         "date_unixtime": "1755000120", "text": "", "media_type": "voice_message"},
    ]}
    p = intake.parse(json.dumps(data, ensure_ascii=False))
    assert p.kind == intake.TG_JSON
    assert len(p.lines) == 3                      # сервисная запись пропущена
    assert p.lines[1].text == "смотри пример.рф"  # куски разметки склеены
    assert p.lines[2].has_media and p.lines[2].media_kind == "voice"
    assert any("сервисных" in n for n in p.notes)


def test_empty_input_yields_nothing():
    p = intake.parse("   \n  ")
    assert not p.ok and p.lines == []


def test_guess_roles_marks_me_as_owner():
    p = intake.parse(TG_COPY)
    guess = intake.guess_roles(p)
    assert guess["Я"] == roles.OWNER
    assert guess["Анастасия"] == roles.CLIENT


# ---------- запись ----------

@pytest.fixture(autouse=True)
def isolated_projects_dir(tmp_path, monkeypatch):
    """Свой projects/ на каждый тест.

    База пересоздаётся между тестами, поэтому id проектов начинаются заново,
    а каталоги — нет: `find(1)` находил папку от предыдущего теста. В бою
    такого не бывает (id не переиспользуются), но тесты бы врали.
    """
    monkeypatch.setattr(cfg, "projects_dir", tmp_path / "projects")


@pytest.fixture
async def project(db):
    await make_account("qorsa")
    return await intake.create_project(account_code="qorsa", client="Анастасия",
                                       title="TUR TUK")


async def test_create_project_makes_its_own_folder(project):
    assert project.status == "briefing"
    # Галочку менеджера ставим сразу: заказ завёл сам владелец, второго
    # подтверждения от того же человека спрашивать незачем
    assert project.ready_for_work is True
    folder = projectdir.find(project.id)
    assert folder is not None and folder.name == f"p{project.id}-tur-tuk"
    for sub in ("workspace", "intake", "docs", "logs"):
        assert (folder / sub).is_dir()


async def test_apply_writes_messages_with_roles(project):
    p = intake.parse(TG_COPY)
    stats = await intake.apply(project.id, p, intake.guess_roles(p),
                               source=TG_COPY)
    assert stats["added"] == 5
    assert stats["roles"] == {roles.CLIENT: 3, roles.OWNER: 2}

    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage).where(ChatMessage.project_id == project.id)
            .order_by(ChatMessage.created_at))).scalars().all()
    assert [r.sender_role for r in rows] == [
        roles.CLIENT, roles.OWNER, roles.CLIENT, roles.OWNER, roles.CLIENT]
    # порядок реплик обязан быть строгим: бриф читает их по created_at,
    # а «подтверждённое предложение» держится на том, что согласие идёт ПОСЛЕ
    assert [r.created_at for r in rows] == sorted(r.created_at for r in rows)


async def test_transcript_is_kept_in_project_intake(project):
    p = intake.parse(COLONS)
    await intake.apply(project.id, p, intake.guess_roles(p))
    saved = list((projectdir.find(project.id) / "intake").glob("*.txt"))
    assert len(saved) == 1
    body = saved[0].read_text(encoding="utf-8")
    assert "[client] Анастасия: нужен интернет-магазин косметики" in body
    assert "[owner] Я: предлагаю WordPress" in body


async def test_saved_transcript_has_no_secret_values(project, tmp_path):
    """Копия выгрузки НЕ должна обходить перехват.

    В первой версии в intake/ ложился текст ровно в том виде, в каком его
    вставили: в базу пароль не попадал, а на диск попадал. Перехват, который
    обходится записью рядом, перехватом не является.
    """
    import autopilot.secrets_scan as scan
    from cryptography.fernet import Fernet

    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    old, scan.default_vault = scan.default_vault, v
    try:
        raw = ("Анастасия: пароль: Sup3rSecret!42\nЯ: принял\n"
               "Анастасия: ещё раз\nЯ: понял")
        p = intake.parse(raw)
        await intake.apply(project.id, p, intake.guess_roles(p))
    finally:
        scan.default_vault = old

    body = "\n".join(f.read_text(encoding="utf-8")
                     for f in (projectdir.find(project.id) / "intake").glob("*.txt"))
    assert "Sup3rSecret!42" not in body
    assert "{{SECRET:" in body


async def test_raw_export_is_not_copied_into_every_message(project):
    """В raw_json идёт пометка о происхождении, а не сама выгрузка.

    Иначе весь вставленный текст размножается по числу реплик — вместе
    с тем, что перехватчик из них уже вырезал.
    """
    p = intake.parse(COLONS)
    await intake.apply(project.id, p, intake.guess_roles(p), source="дашборд")
    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage.raw_json)
            .where(ChatMessage.project_id == project.id))).scalars().all()
    for raw in rows:
        assert raw["source"] == "дашборд"
        assert "интернет-магазин" not in json.dumps(raw, ensure_ascii=False)


async def test_second_paste_of_same_export_adds_nothing(project):
    p = intake.parse(TG_COPY)
    first = await intake.apply(project.id, p, intake.guess_roles(p))
    second = await intake.apply(project.id, intake.parse(TG_COPY),
                                intake.guess_roles(p))
    assert first["added"] == 5
    assert second["added"] == 0 and second["skipped"] == 5
    assert await intake.message_count(project.id) == 5


async def test_repeated_short_replies_are_not_collapsed(project):
    """Два честных «да» подряд — два сообщения, а не одно.

    Идентификатор реплики считается из содержимого, и без позиции в хеше
    дедупликация съедала бы повторы, которые клиент правда написал дважды.
    """
    raw = "Клиент: да\nЯ: точно?\nКлиент: да\nЯ: понял\nКлиент: ага"
    p = intake.parse(raw)
    stats = await intake.apply(project.id, p, {"Клиент": roles.CLIENT, "Я": roles.OWNER})
    assert stats["added"] == 5


async def test_secrets_are_intercepted_before_the_database(project, tmp_path):
    """Пароль из старой переписки не оказывается в базе открытым."""
    import autopilot.secrets_scan as scan
    from cryptography.fernet import Fernet

    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    old, scan.default_vault = scan.default_vault, v
    try:
        raw = ("Анастасия: вот доступ, пароль: Sup3rSecret!42\n"
               "Я: принял\nАнастасия: ещё логин admin\nЯ: ок")
        p = intake.parse(raw)
        await intake.apply(project.id, p, {"Анастасия": roles.CLIENT, "Я": roles.OWNER})
    finally:
        scan.default_vault = old

    async with Session() as s:
        texts = (await s.execute(
            select(ChatMessage.text)
            .where(ChatMessage.project_id == project.id))).scalars().all()
    joined = "\n".join(texts)
    assert "Sup3rSecret!42" not in joined
    assert "{{SECRET:" in joined
    assert "Sup3rSecret!42" in v.values()


async def test_dry_run_touches_neither_base_nor_vault(project, tmp_path):
    import autopilot.secrets_scan as scan
    from cryptography.fernet import Fernet

    v = Vault(path=tmp_path / "s.enc", key=Fernet.generate_key())
    old, scan.default_vault = scan.default_vault, v
    try:
        p = intake.parse("Анастасия: пароль: Sup3rSecret!42\nЯ: ок\n"
                         "Анастасия: и ещё\nЯ: понял")
        stats = await intake.apply(project.id, p, intake.guess_roles(p), store=False)
    finally:
        scan.default_vault = old

    assert stats["added"] == 4
    assert await intake.message_count(project.id) == 0
    assert v.values() == []


async def test_pasted_chat_gets_its_own_transport(project):
    """Транспорт «paste», а не «telegram»: живого канала у этого чата нет,
    и притворяться телеграмом значит однажды попробовать в него ответить."""
    p = intake.parse(COLONS)
    await intake.apply(project.id, p, intake.guess_roles(p))
    async with Session() as s:
        chat = (await s.execute(
            select(ProjectChat).where(
                ProjectChat.project_id == project.id))).scalars().first()
    assert chat.transport == intake.TRANSPORT
    assert chat.chat_id == intake.chat_id_for(project.id)


async def test_unknown_company_is_refused(db):
    await make_account("qorsa")
    with pytest.raises(ValueError, match="hustle"):
        await intake.create_project(account_code="hustle", client="к", title="т")


async def test_project_without_folder_name_still_gets_one(db):
    await make_account("qorsa")
    p = await intake.create_project(account_code="qorsa", client="",
                                    title="!!!")
    folder = projectdir.find(p.id)
    assert folder is not None and folder.name == f"p{p.id}"
