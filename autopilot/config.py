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

    # Суточный потолок РЕАЛЬНЫХ денег. Расход подписки сюда не входит:
    # иначе бюджет вставал бы на пустом месте, когда всё идёт через CLI
    # и ни рубля не тратится.
    daily_budget_usd = _f("DAILY_BUDGET_USD", 25)

    # Суточный потолок РАСХОДА ПОДПИСКИ — второй тормоз, и без него после
    # перевода исполнителя на CLI у полосы build не остаётся никакого
    # ограничителя вовсе: денежный счётчик на подписке всегда показывает ноль.
    #
    # Цифра — ОЦЕНКА, которую отдаёт сам CLI («во что это обошлось бы на API»),
    # а не списание. Мерить квоту точнее нечем: пятичасовое окно снаружи
    # не видно. Ориентир для порога: день работы с брифом и планом по одному
    # живому проекту дал ~$6 при 13 вызовах и 103 минутах.
    daily_cli_budget_usd = _f("DAILY_CLI_BUDGET_USD", 25)

    # --- откуда берутся ответы модели ---
    # api — платный SDK, cli — подписка через `claude -p`.
    # По умолчанию cli: денег на API нет, работа идёт по подписке.
    llm_backend = os.getenv("LLM_BACKEND", "cli").strip().lower()
    llm_backend_brief = os.getenv("LLM_BACKEND_BRIEF", "").strip().lower()
    llm_backend_plan = os.getenv("LLM_BACKEND_PLAN", "").strip().lower()
    llm_backend_judge = os.getenv("LLM_BACKEND_JUDGE", "").strip().lower()
    # Модель для CLI. sonnet намеренно: недельная квота Opus на порядок
    # меньше и нужна владельцу для ручной работы в Claude Code.
    cli_model = os.getenv("CLI_MODEL", "sonnet")
    # 600 не хватало: замерено, что CLI отвечает в 3.5 раза дольше API
    # (245 с против 70 с на брифе), а план по ТЗ из 32 пунктов на API
    # занимал 240 с — то есть на CLI около 800 с. Прогон падал
    # с «не ответил за 600s», хотя модель работала штатно.
    cli_timeout_sec = _i("CLI_TIMEOUT_SEC", 1800)

    # Пауза после упора в квоту подписки, если CLI не сказал время сброса.
    # Растёт по степени двойки до потолка: ломиться в закрытую дверь раз
    # в минуту — способ не заметить, что она закрыта.
    limit_backoff_start_sec = _i("LIMIT_BACKOFF_START_SEC", 300)
    limit_backoff_max_sec = _i("LIMIT_BACKOFF_MAX_SEC", 3600)
    # уведомление владельцу об упоре — не чаще раза за этот период
    limit_notify_every_sec = _i("LIMIT_NOTIFY_EVERY_SEC", 3600)

    # --- окно подписки: автопилот против ручной работы владельца ---
    # Headless-запуски Claude Code едят ТУ ЖЕ пятичасовую квоту, что и
    # работа владельца руками. Разводим по времени и по числу сессий.
    # Равные границы = ограничение выключено.
    quiet_build_start = _i("QUIET_HOURS_BUILD_START", 0)
    quiet_build_end = _i("QUIET_HOURS_BUILD_END", 0)
    # Отдельно от LANE_BUILD: полоса считает ЗАДАЧИ, а этот потолок —
    # одновременные процессы claude, включая те, что заняты приёмкой.
    cc_max_concurrent = _i("CC_MAX_CONCURRENT", 2)

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
    # Заводить проект только для строк с заполненным «Чат клиента».
    #
    # По умолчанию ВКЛЮЧЕНО, и это осознанно. Без него первая же запись
    # проставила бы ID во все строки листа разом — массовая правка, которую
    # потом откатывать руками. С ним заказы подхватываются по одному, ровно
    # по мере того, как в них появляется чат: до этого момента боту всё равно
    # нечего делать с заказом, писать по нему некуда.
    sheet_require_chat = _b("SHEET_REQUIRE_CHAT", True)

    tg_token = os.getenv("TG_BOT_TOKEN", "")
    tg_owner = os.getenv("TG_OWNER_CHAT_ID", "")       # я, техчасть
    tg_manager = os.getenv("TG_MANAGER_CHAT_ID", "")   # менеджер: деньги, сроки, объём
    tg_poll_timeout = _i("TG_POLL_TIMEOUT", 25)        # long polling getUpdates
    # Личка через Telegram Business выключена: работа переехала в групповые чаты.
    # Код бизнес-режима оставлен на случай возврата — см. CLAUDE.md
    tg_business_enabled = _b("TG_BUSINESS_ENABLED", False)

    # --- кто есть кто в группе ---
    # Менеджер и владелец — один человек с одного аккаунта, поэтому
    # MANAGER_TG_ID по умолчанию просто алиас OWNER_TG_ID. Роль manager
    # осталась в перечислении: включи MANAGER_SEPARATE=1, если менеджер
    # когда-нибудь отделится и сядет в группу со своего аккаунта.
    owner_tg_id = os.getenv("OWNER_TG_ID", "")
    manager_separate = _b("MANAGER_SEPARATE", False)
    manager_tg_id = os.getenv("MANAGER_TG_ID", "")
    bot_tg_id = os.getenv("BOT_TG_ID", "")
    bot_username = os.getenv("BOT_USERNAME", "").lstrip("@")
    owner_max_id = os.getenv("OWNER_MAX_ID", "")
    manager_max_id = os.getenv("MANAGER_MAX_ID", "")
    bot_max_id = os.getenv("BOT_MAX_ID", "")

    # менеджер называет группы по шаблону — по нему и опознаём проект
    group_name_template = os.getenv("GROUP_NAME_TEMPLATE", "Qorsa • {client} • {project}")
    group_match_threshold = _f("GROUP_MATCH_THRESHOLD", 0.78)

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

    # --- brief.py ---
    brief_model = os.getenv("BRIEF_MODEL", "") or os.getenv("JUDGE_MODEL", "claude-sonnet-4-6")
    brief_min_confidence = _f("BRIEF_MIN_CONFIDENCE", 0.75)
    brief_question_cooldown_h = _f("BRIEF_QUESTION_COOLDOWN_H", 12)
    brief_full_rebuild_every = _i("BRIEF_FULL_REBUILD_EVERY", 50)
    # Бюджет контекста переписки в ТОКЕНАХ, а не в сообщениях.
    #
    # Раньше стоял счёт по сообщениям (последние 80 целиком, остальные обрезком
    # в 120 символов), и на первой же живой переписке это выбросило главное:
    # заполненный клиентом бриф на 3851 символ оказался 53-м с конца и дошёл
    # до модели в виде 120 знаков. Весь чат при этом весил 15 тысяч символов
    # и влезал целиком. Считать надо объём, а не количество реплик.
    brief_context_tokens = _i("BRIEF_CONTEXT_TOKENS", 60000)
    brief_max_attempts = _i("BRIEF_MAX_ATTEMPTS", 2)
    # Потолок ответа модели. 4000 не хватало: на полной переписке JSON брифа
    # обрывался на середине и падал разбор — «не-JSON» вместо «не поместилось»
    brief_max_tokens = _i("BRIEF_MAX_TOKENS", 16000)
    # сколько вопросов держим в брифе всего
    brief_max_questions = _i("BRIEF_MAX_QUESTIONS", 8)
    # и сколько отдаём клиенту за один раз: список из восьми он не читает
    brief_questions_per_message = _i("BRIEF_QUESTIONS_PER_MESSAGE", 3)
    # на сколько сообщений вперёд ищем согласие клиента на предложение владельца
    confirm_window = _i("CONFIRM_WINDOW", 3)
    # сколько раз собирать бриф за один проход. >1 дорого, но лечит разброс
    # модели: пункт входит в итог, если встретился хотя бы в одном прогоне
    # По умолчанию ОДИН прогон. На API три прогона стоили втрое больше
    # денег, на подписке — втрое больше квоты пятичасового окна,
    # а окно кончается быстрее кошелька. Разброс модели гасит
    # накопление пунктов: потерянное в одном прогоне вернётся в другом.
    brief_samples = _i("BRIEF_SAMPLES", 1)
    # дешёвая модель для сверки брифа с эталонным списком требований
    coverage_model = os.getenv("COVERAGE_MODEL", "claude-haiku-4-5-20251001")

    # --- planner.py ---
    plan_model = os.getenv("PLAN_MODEL", "") or os.getenv("JUDGE_MODEL", "claude-sonnet-4-6")
    plan_max_attempts = _i("PLAN_MAX_ATTEMPTS", 2)
    # На 32 пунктах ТЗ план в 8000 токенов не помещался и обрывался
    # на середине JSON — выглядело как «модель отдала мусор»
    # Выше ~16k SDK требует стриминга: он считает, что запрос займёт
    # больше десяти минут, и отказывается идти без него
    plan_max_tokens = _i("PLAN_MAX_TOKENS", 16000)
    # ниже этой доли auto-задач проект малопригоден для автопилота
    autonomy_min_ratio = _f("AUTONOMY_MIN_RATIO", 0.5)


cfg = Config()
cfg.workspaces.mkdir(parents=True, exist_ok=True)
cfg.logs.mkdir(parents=True, exist_ok=True)
