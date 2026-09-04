"""Отчёт о цикле: во что обходится работа и где она буксует.

    python scripts/loop_report.py                # за 30 дней
    python scripts/loop_report.py --days 7
    python scripts/loop_report.py --json         # машинно-читаемо

Ничего не пишет и ничего не решает — только читает базу. Все цифры считаются
по уже накопленным данным; то, чего в базе нет, отдельным разделом в конце,
а не пустой строкой. Пустая цифра неотличима от нуля, и это самый простой
способ соврать отчётом.

Двух вещей здесь нет намеренно: взвешенного «балла пользы» (коэффициенты
берутся с потолка, а оптимизировать по такой цифре начинают всерьёз)
и суммы «деньги плюс подписка» — они меряют разные ресурсы, и складывать
их значит соврать в обе стороны сразу.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from autopilot import metrics                                   # noqa: E402
from autopilot.db import init_db                                # noqa: E402

RU_KIND = {"execute": "сессии агента", "judge": "судья", "brief": "сбор ТЗ",
           "plan": "планирование", "prompt": "задание агенту",
           "diagnose": "разбор застревания"}

RU_STATUS = {"done": "закрыто", "ready": "в очереди", "pending": "ждёт зависимостей",
             "running": "в работе", "escalated": "эскалация",
             "needs_human": "ждёт разбора", "awaiting_keys": "ждёт ключа"}

RU_OUTCOME = {"failed": "провал", "done": "принято", "built": "собрано",
              "escalated": "эскалация", "stagnation": "застревание",
              "policy": "нарушение политики", "limit": "упор в квоту"}

RU_ACCEPT = {"proved": "доказано кодом", "by_model": "на слово модели",
             "by_owner": "закрыл владелец руками",
             "tainted": "проверку правил сам агент",
             "unknown": "состав приёмки не записан"}


def bar(value: float, total: float, width: int = 28) -> str:
    filled = int(round(width * value / total)) if total else 0
    return "█" * filled + "·" * (width - filled)


def report(data: dict) -> str:
    s, b, c, loop = data["spend"], data["blind_spot"], data["cost"], data["loop"]
    out: list[str] = []
    add = out.append

    add(f"═══ РАСХОД за {s['days']} дн. ═══")
    add(f"  настоящие деньги   ${s['money_usd']:.2f} из ${data['budgets']['money']:.0f}/сут")
    add(f"  оценка подписки   ~${s['subscription_usd']:.2f} из "
        f"${data['budgets']['subscription']:.0f}/сут")
    add("  (не складываются: это разные ресурсы — кошелёк и пятичасовое окно)")
    add(f"  вызовов всего      {s['runs']}"
        + (f", НЕ ВЕРНУЛОСЬ {s['unfinished']}" if s["unfinished"] else ""))
    for kind, v in s["by_kind"].items():
        add(f"    {RU_KIND.get(kind, kind):22s} {v['runs']:4d} шт  "
            f"${v['usd']:8.2f}  {v['minutes']:7.1f} мин")

    add("")
    add("═══ СЛЕПАЯ ЗОНА СЧЁТЧИКА ═══")
    add(f"  сессий исполнителя {b['runs']}, из них без измеренной цены {b['blind_runs']}")
    add(f"  времени мимо счётчика: {b['blind_minutes']:.0f} из {b['minutes']:.0f} мин "
        f"({b['blind_time_pct']}%) при ${b['blind_usd']:.2f} учтённых")
    if b["blind_time_pct"] > 20:
        add("  → денежным потолкам этой доли не видно. Держит её только потолок")
        add(f"    по ЧИСЛУ СЕССИЙ ({data['budgets']['sessions']}/сут).")

    add("")
    add("═══ ЦЕНА ЗАКРЫТОЙ ЗАДАЧИ ═══")
    add(f"  закрыто задач {c['closed_tasks']}, из них с расходом {c['priced']}")
    add(f"  медиана ${c['median_usd']:.2f} · среднее ${c['mean_usd']:.2f} · "
        f"максимум ${c['max_usd']:.2f}")
    add(f"  ушло в задачи, которые ждут человека: ${c['stuck_usd']:.2f} "
        f"({c['stuck_pct']}% расхода)")

    add("")
    add("═══ ФОРМА ЦИКЛА ═══")
    add(f"  задач {loop['tasks']}, сессий агента {loop['sessions']}, "
        f"впустую {loop['wasted_sessions']} ({loop['wasted_pct']}%)")
    if loop["sessions_hist"]:
        add("  сессий на задачу:")
        top = max(loop["sessions_hist"].values())
        for n, count in loop["sessions_hist"].items():
            mark = "  ← сюда стоит посмотреть глазами" if n >= 10 else ""
            add(f"    {n:3d} сессий {bar(count, top)} {count}{mark}")
    if loop["statuses"]:
        add("  по статусам: " + ", ".join(
            f"{RU_STATUS.get(k, k)} {v}" for k, v in loop["statuses"].items()))
    if loop["ledger"]:
        add("  журнал попыток: " + ", ".join(
            f"{RU_OUTCOME.get(k, k)} {v}" for k, v in loop["ledger"].items()))
        add(f"  (ведётся с {loop['ledger_since']})")

    acc = data["acceptance"]
    add("")
    add("═══ ЧЕМ ПРИНИМАЛИСЬ ЗАДАЧИ ═══")
    if acc:
        for k, v in sorted(acc.items(), key=lambda kv: -kv[1]):
            add(f"  {RU_ACCEPT.get(k, k):32s} {v}")
    else:
        add("  закрытых задач нет")

    add("")
    add("═══ ЧЕГО В БАЗЕ НЕТ ═══")
    add("  (пустая цифра выше — это «не записано», а не «ноль»)")
    for line in data["gaps"]["missing"]:
        add(f"  • {line}")
    return "\n".join(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description="отчёт о работе цикла")
    ap.add_argument("--days", type=int, default=30, help="окно расхода, дней")
    ap.add_argument("--json", action="store_true", help="машинно-читаемо")
    args = ap.parse_args()

    await init_db()
    data = await metrics.overview(args.days)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
