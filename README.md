# Qorsa Autopilot — скелет

Локальный оркестратор: Google-таблица заказов → задачи → Claude Code → верификация → сообщение клиенту.
Никакого Redis/Celery: один asyncio-процесс + SQLite. Этого хватает до ~50 параллельных проектов.

## Запуск

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  на *nix — source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m autopilot.main
```

**Все команды запускай из активированного venv.** Зависимости стоят только
в нём: системный `python` их не видит и падает с `ModuleNotFoundError`.
Если активировать окружение не хочется, зови интерпретатор напрямую:

```bash
.venv/Scripts/python.exe -m autopilot.main      # на *nix — .venv/bin/python
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe scripts/brief_eval.py --list
```

Ключ Anthropic ищется в трёх местах по порядку: **хранилище secrets.enc**,
переменная окружения, файл `.env`. Хранилище надёжнее всего:

```bash
.venv/Scripts/python.exe scripts/vault_cli.py genkey     # положи в .env как VAULT_KEY
.venv/Scripts/python.exe scripts/vault_cli.py add ANTHROPIC_API_KEY
```

Заглушку `sk-ant-...` из `.env.example` код настоящим ключом не считает.

## Google-таблица

Заведи лист с шапкой в первой строке. Порядок колонок любой, важны названия.

**Твои колонки** (бот только читает):
`ID` · `Клиент` · `Проект` · `TG chat` · `Цена` · `Дедлайн` · `Приоритет` · `Папка`

**Колонки бота** (перезаписываются, руками не трогай):
`Статус` · `Прогресс` · `Превью` · `Последнее действие` · `Стоимость $` · `Обновлено`

`ID` оставь пустым для новых строк — бот проставит сам.
`Приоритет`: 1 высокий, 2 обычный, 3 фон.

Доступ: заведи service account в Google Cloud, скачай `service_account.json`,
дай его email права редактора на таблицу.

## Как раздаётся время

Три полосы с разным параллелизмом (`LANE_CHAT` / `LANE_BUILD` / `LANE_VERIFY`).
Ответы клиентам живут в `chat` и не ждут за компиляцией.

Внутри полосы — weighted fair queueing: берётся проект с минимальным
`served_units / weight`. Вес = приоритет × буст по дедлайну (×2 если ≤3 дней, ×4 если просрочено).
Один проект не может занять два build-слота одновременно.

## Предохранители

- `DAILY_BUDGET_USD` — при превышении build/verify встают, chat работает
- `MAX_ATTEMPTS` — после N провалов задача уходит в `escalated`, проект в `blocked`
- 🔴-гейт в `communicator.py` — сообщения про цену/сроки/договор не уходят без тебя

## Что дописать дальше

1. `ingest.py` — Telegram Business API → таблица `messages`
2. `brief.py` — чат → JSON ТЗ (обязательно `confidence` и `open_questions`)
3. `planner.py` — ТЗ → задачи с machine-checkable acceptance
4. `checks/` — Playwright-сценарии (375/768/1440, горизонтальный скролл, консоль)
