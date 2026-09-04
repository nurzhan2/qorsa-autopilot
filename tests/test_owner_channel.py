"""Канал до владельца: и туда, и обратно.

Живой случай, ради которого это написано. Проект 8 простоял шесть дней,
и никто не заметил — потому что уведомлять владельца было НЕЧЕМ:
`TG_OWNER_CHAT_ID` пуст, а id владельца живёт по компаниям в
`accounts.toml` (qorsa 7905565788, hustle 8649452219). Глобал остался
от одноаккаунтной версии.

Поломка была симметричной, и вторая половина опаснее первой: те же три
строки `str(msg.chat_id) != str(cfg.tg_owner)` в `ingest` означали, что
`/bind`, `/done` и `/confirm` от владельца не срабатывали никогда — при
том что текст уведомления дословно говорит «подтвердить — /confirm 17».

Существующие тесты этого не показали и показать не могли: они
монкейпатчат `cfg.tg_owner` и строят `Communicator` без компаний, то есть
проверяют ровно запасной путь. Здесь компании передаются.
"""
from __future__ import annotations

from conftest import make_project

from autopilot import roles
from autopilot.accounts import Account
from autopilot.communicator import (TO_MANAGER, TO_OWNER, Communicator,
                                    internal_recipient)
from autopilot.config import cfg
from autopilot.ingest import _from_owner
from autopilot.transports.base import InboundMessage

QORSA = Account(code="qorsa", name="Qorsa Studio", owner_tg_id="7905565788",
                bot_token_ref="QORSA_BOT_TOKEN")
HUSTLE = Account(code="hustle", name="Hustle Design", owner_tg_id="8649452219",
                 bot_token_ref="HUSTLE_BOT_TOKEN")


class FakeTransport:
    """Транспорт компании. Помнит, ЧЕЙ он и куда писал."""

    name = "telegram"

    def __init__(self, account: str):
        self.account = account
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str, connection_id=None) -> str:
        self.sent.append((str(chat_id), text))
        return "1"

    def supports_impersonation(self) -> bool:
        return False


class Spy:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, chat_id: str, text: str) -> None:
        self.sent.append((str(chat_id), text))

    def to(self, chat_id: str) -> list[str]:
        return [t for c, t in self.sent if c == str(chat_id)]


# ---------- адрес владельца берётся у КОМПАНИИ ----------

def test_адрес_владельца_из_компании(monkeypatch):
    monkeypatch.setattr(cfg, "tg_owner", "")          # как в живом .env
    comm = Communicator(accounts=[QORSA, HUSTLE])
    assert comm.owner_chat("qorsa") == "7905565788"
    assert comm.owner_chat("hustle") == "8649452219"


def test_единственная_компания_подставляется_без_имени(monkeypatch):
    """Компания не названа, а настроена одна — гадать не о чем."""
    monkeypatch.setattr(cfg, "tg_owner", "")
    comm = Communicator(accounts=[HUSTLE])
    assert comm.owner_chat() == "8649452219"
    assert comm.owner_chat("нет-такой") == "8649452219"


def test_без_компаний_остаётся_глобал(monkeypatch):
    """Установка без accounts.toml обязана продолжать работать."""
    monkeypatch.setattr(cfg, "tg_owner", "owner-chat")
    assert Communicator().owner_chat("qorsa") == "owner-chat"


async def test_бот_и_адрес_из_ОДНОЙ_компании(monkeypatch):
    """Перекрёстная пара «бот Qorsa → чат владельца Hustle» даёт 403.

    Это ровно тот класс ошибок, против которого написан CrossAccountSend,
    и раньше `notify_owner` его допускал: адрес брался глобальный, а
    транспорт — «любой первый telegram».
    """
    monkeypatch.setattr(cfg, "tg_owner", "")
    q, h = FakeTransport("qorsa"), FakeTransport("hustle")
    comm = Communicator(transports=[q, h], accounts=[QORSA, HUSTLE])

    await comm.notify_owner("по заказу Hustle", account="hustle")

    assert h.sent == [("8649452219", "по заказу Hustle")]
    assert q.sent == [], "письмо ушло ботом чужой компании"


async def test_компания_берётся_из_проекта(db, monkeypatch):
    """Звать можно проектом — код компании возьмётся из него сам."""
    monkeypatch.setattr(cfg, "tg_owner", "")
    project = await make_project(account="hustle")
    q, h = FakeTransport("qorsa"), FakeTransport("hustle")
    comm = Communicator(transports=[q, h], accounts=[QORSA, HUSTLE])

    await comm.notify_owner("эскалация", project=project)

    assert h.sent and h.sent[0][0] == "8649452219"
    assert q.sent == []


async def test_очередь_к_владельцу_тоже_адресована_компанией(db, monkeypatch):
    """Не только notify_owner: ВСЯ очередь TO_OWNER шла в литерал «OWNER».

    Туда уходит коммерция и техника клиента — «сколько стоит доработка»,
    «FTP не пускает». Починка одного notify_owner оставила бы этот канал
    мёртвым, а он больше.
    """
    monkeypatch.setattr(cfg, "tg_owner", "")
    monkeypatch.setattr(cfg, "manager_separate", False)
    project = await make_project(account="hustle", tg_chat_id="client-chat")
    spy = Spy()
    comm = Communicator(send_fn=spy, accounts=[QORSA, HUSTLE])

    m = await comm.incoming(project, "сколько будет стоить доработка?")
    assert m.route == TO_OWNER
    assert await comm.pump_once() == 1

    assert spy.to("OWNER") == [], "адресат снова литерал, а не чат"
    assert len(spy.to("8649452219")) == 1


async def test_отказ_чата_объясняется_словами(monkeypatch, caplog):
    """«Бот не может написать первым» — самый частый отказ, и он не чинится
    кодом. Проверено живьём 05.09: оба бота подняты, `getMe` проходит, а
    `getChat` на оба id владельца отвечает 400 «chat not found» — то есть
    владелец ни у одного из ботов не нажимал Start.

    Раньше это уезжало в голый `log.exception` и выглядело как ещё один
    трейсбэк в шуме. Уведомление, о недоставке которого известно только
    по трейсбэку, — та же тишина, только тише.
    """
    import logging

    class Отказ(Exception):
        class response:                       # noqa: N801  (мимикрия под httpx)
            status_code = 400
            text = '{"ok":false,"description":"Bad Request: chat not found"}'

    class Немой(FakeTransport):
        async def send(self, chat_id, text, connection_id=None):
            raise Отказ()

    monkeypatch.setattr(cfg, "tg_owner", "")
    comm = Communicator(transports=[Немой("hustle")], accounts=[HUSTLE])
    with caplog.at_level(logging.ERROR):
        await comm.notify_owner("эскалация", account="hustle")

    сказано = "\n".join(r.getMessage() for r in caplog.records)
    assert "НЕ МОГУ ПИСАТЬ ВЛАДЕЛЬЦУ" in сказано
    assert "Start" in сказано
    assert "8649452219" in сказано


def test_менеджер_падает_на_владельца_пока_он_один(monkeypatch):
    """MANAGER_SEPARATE=0 значит «менеджер это владелец» — так записано
    в roles.py. В адресации этого не было, и спорное уезжало в «MANAGER»."""
    monkeypatch.setattr(cfg, "manager_separate", False)
    monkeypatch.setattr(cfg, "tg_owner", "")
    monkeypatch.setattr(cfg, "tg_manager", "")
    assert internal_recipient(TO_MANAGER, owner="8649452219") == "8649452219"


def test_отдельный_менеджер_владельцем_не_подменяется(monkeypatch):
    """А при MANAGER_SEPARATE=1 это разные люди, и слить их нельзя."""
    monkeypatch.setattr(cfg, "manager_separate", True)
    monkeypatch.setattr(cfg, "tg_manager", "")
    assert internal_recipient(TO_MANAGER, owner="8649452219") == "MANAGER"


# ---------- обратный конец: команды владельца ----------

def _msg(text: str, chat_id: str = "чат") -> InboundMessage:
    return InboundMessage(transport="telegram", chat_id=chat_id,
                          message_id=1, sender_id="7905565788", text=text)


def test_команда_владельца_опознаётся_по_роли(monkeypatch):
    """Главная поломка обратного конца: при пустом cfg.tg_owner ни одна
    команда не срабатывала, и уведомление звало /confirm впустую."""
    monkeypatch.setattr(cfg, "tg_owner", "")
    assert _from_owner(_msg("/confirm 17"), roles.OWNER) is True


def test_команда_клиента_не_проходит(monkeypatch):
    """Клиент, написавший «/confirm 17», задачу не закрывает."""
    monkeypatch.setattr(cfg, "tg_owner", "")
    assert _from_owner(_msg("/confirm 17"), roles.CLIENT) is False
    assert _from_owner(_msg("/confirm 17"), "") is False


def test_глобальный_чат_остаётся_запасным(monkeypatch):
    """Установка без accounts.toml: там владелец задан именно так."""
    monkeypatch.setattr(cfg, "tg_owner", "owner-chat")
    assert _from_owner(_msg("/done 5", chat_id="owner-chat"), "") is True
    assert _from_owner(_msg("/done 5", chat_id="чужой"), "") is False
