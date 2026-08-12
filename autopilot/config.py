import logging
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

log = logging.getLogger("cfg")


def _num(key, default, cast):
    """Пустая или кривая переменная в .env не должна ронять импорт всего процесса."""
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return cast(default)
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("%s=%r — не число, беру значение по умолчанию %s", key, raw, default)
        return cast(default)


def _i(k, d): return _num(k, d, int)
def _f(k, d): return _num(k, d, float)
def _b(k, d=False): return str(os.getenv(k, str(d))).strip().lower() in ("1", "true", "yes", "on")


class Config:
    root = ROOT
    db_url = os.getenv("DB_URL", f"sqlite+aiosqlite:///{ROOT}/autopilot.db")
    workspaces = Path(os.getenv("WORKSPACES") or ROOT / "workspaces")
    logs = Path(os.getenv("LOGS") or ROOT / "logs")

    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    cc_model = os.getenv("CC_MODEL", "sonnet")
    judge_model = os.getenv("JUDGE_MODEL", "claude-sonnet-4-6")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    max_turns = _i("MAX_TURNS", 40)
    max_attempts = _i("MAX_ATTEMPTS", 4)
    task_timeout_sec = _i("TASK_TIMEOUT_SEC", 1800)

    daily_budget_usd = _f("DAILY_BUDGET_USD", 25)

    lane_limits = {
        "chat": _i("LANE_CHAT", 8),
        "build": _i("LANE_BUILD", 3),
        "verify": _i("LANE_VERIFY", 2),
    }
    # build/verify — эксклюзивные: один проект не может занимать две такие задачи разом
    lane_exclusive = {"chat": False, "build": True, "verify": True}

    # тихие часы отправки, ЛОКАЛЬНОЕ время; равные границы = тишина выключена
    quiet_start = _i("QUIET_START", 23)
    quiet_end = _i("QUIET_END", 9)

    # заглушки вместо Claude Code и судьи: гонять планировщик вживую, ничего не тратя
    dry_run = _b("DRY_RUN", False)

    # клиенту уходит не больше одного отчёта о готовности за это окно
    aggregate_window_min = _i("AGGREGATE_WINDOW_MIN", 60)
    # период полураспада счётчика WFQ: простоявший проект перестаёт быть должником
    served_halflife_h = _f("SERVED_HALFLIFE_H", 24)
    # не чаще раза в сутки напоминаем клиенту про недостающие доступы
    access_reminder_h = _f("ACCESS_REMINDER_H", 24)

    # хранилище секретов: ключ Fernet и файл ВНЕ репозитория
    vault_key = os.getenv("VAULT_KEY", "")
    vault_path = Path(os.getenv("VAULT_PATH") or ROOT / "secrets.enc")

    sheet_id = os.getenv("SHEET_ID", "")
    sheet_tab = os.getenv("SHEET_TAB", "Заказы")
    google_creds = os.getenv("GOOGLE_CREDS", str(ROOT / "service_account.json"))
    sheet_sync_sec = _i("SHEET_SYNC_SEC", 60)

    tg_token = os.getenv("TG_BOT_TOKEN", "")
    tg_owner = os.getenv("TG_OWNER_CHAT_ID", "")       # я, техчасть
    tg_manager = os.getenv("TG_MANAGER_CHAT_ID", "")   # менеджер: деньги, сроки, объём
    tg_poll_timeout = _i("TG_POLL_TIMEOUT", 25)        # long polling getUpdates

    # --- MAX ---
    max_token = os.getenv("MAX_TOKEN", "")
    # справочник dev.max.ru отдаёт platform-api2, часть публикаций — platform-api.
    # Проверь на своём токене и поправь здесь, если не сойдётся
    max_api_base = os.getenv("MAX_API_BASE", "https://platform-api.max.ru")
    max_poll_timeout = _i("MAX_POLL_TIMEOUT", 30)
    # polling — для локальной работы, webhook — для сервера. Одновременно нельзя
    max_mode = os.getenv("MAX_MODE", "polling").strip().lower()
    max_webhook_url = os.getenv("MAX_WEBHOOK_URL", "")

    ingest_enabled = _b("INGEST_ENABLED", True)


cfg = Config()
cfg.workspaces.mkdir(parents=True, exist_ok=True)
cfg.logs.mkdir(parents=True, exist_ok=True)
