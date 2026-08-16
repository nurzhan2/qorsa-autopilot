"""Прогон brief.py по реальным чатам с показом результата рядом с перепиской.

Урок фазы 2: баги детектора вылезли только на живых данных, синтетика их
не показала. Поэтому бриф надо смотреть глазами, а не только тестами.

    python scripts/brief_eval.py --list                 # что есть в базе
    python scripts/brief_eval.py --project 1            # прогнать и показать
    python scripts/brief_eval.py --project 1 --stub     # без вызова модели
    python scripts/brief_eval.py --project 1 --draft    # черновик эталона требований

`--stub` подставляет вместо модели грубый детерминированный экстрактор.
Он НЕ показывает качество модели — он прогоняет весь тракт (evidence, роли,
запрет на деньги, доступы, вопросы) там, где ключа нет.

ЭТАЛОН — это список требований к чату, написанный РУКАМИ, в свободной форме:
tests/fixtures/briefs/<project>.requirements.txt, одно требование в строке.
Сверка отвечает на вопрос «покрыто ли требование хоть одним пунктом брифа»,
а не «совпадает ли текст»: сравнивать свободные формулировки построчно
бессмысленно, модель каждый раз пишет их иначе. Сопоставляет отдельный
дешёвый вызов модели, вердикты — covered / partial / missing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from sqlalchemy import func, select                                    # noqa: E402

from autopilot.brief import (LIST_FIELDS, ORIGIN_CONFIRMED, Brief,      # noqa: E402
                             _msg_key, agreement, check_coverage,
                             missing_items, parse_requirements)
from autopilot.config import cfg
from autopilot.llm import LimitReached, LLMError                                       # noqa: E402
from autopilot.db import consumed_today, ChatMessage, Project, Session, init_db        # noqa: E402
from autopilot.vault import (anthropic_key, anthropic_key_source,      # noqa: E402
                             missing_secret_message)

# INFO у brief: там печатается расход токенов и стоимость прогона.
# Без этого цифры не видно, а понимать их порядок надо до фазы 4.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("brief").setLevel(logging.INFO)

FIXTURES = ROOT / "tests" / "fixtures" / "briefs"
LINE = "─" * 78


# ---------- офлайн-заглушка вместо модели ----------

WANT_RE = re.compile(r"нужн|нужен|нужно|хочу|хотим|сделать|сделай|добавить|"
                     r"должен|должно|требуется|надо", re.IGNORECASE)
STACK_RE = re.compile(r"wordpress|битрикс|tilda|тильда|react|vue|django|laravel|"
                      r"php|python|node|1c|1с|shopify|opencart", re.IGNORECASE)
ACCESS_RE = re.compile(r"\b(ftp|ssh|git|хостинг|панел|домен|api|аналитик|метрик|"
                       r"макет|фигма|figma|контент)\w*", re.IGNORECASE)
ACCESS_KIND = {
    "ftp": "ftp", "ssh": "ssh", "git": "git", "хостинг": "hosting_panel",
    "панел": "hosting_panel", "домен": "domain", "api": "api_key",
    "аналитик": "analytics", "метрик": "analytics", "макет": "design",
    "фигма": "design", "figma": "design", "контент": "content",
}


class StubModel:
    """Грубый экстрактор без сети. Нужен, чтобы прогнать тракт целиком."""

    def __init__(self):
        self.messages = self

    async def create(self, *, model, max_tokens, system=None, messages, **kw):
        prompt = messages[0]["content"]
        client_lines = []
        parsed = []          # (ключ, роль, текст) в порядке переписки
        for line in prompt.splitlines():
            m = re.match(r"\[([^\]]+)\]\s+(\w+):\s*(.*)", line)
            if not m:
                continue
            parsed.append((m.group(1), m.group(2), m.group(3).strip()))
            if m.group(2) == "client":
                client_lines.append((m.group(1), m.group(3).strip()))

        # предложения владельца, на которые клиент ответил согласием или отказом:
        # ссылаемся на ОБА сообщения, дальше код сам решит, что с ними делать
        proposals = []
        for i, (key, role, line_text) in enumerate(parsed):
            if role != "client" or agreement(line_text) is None:
                continue
            for j in range(i - 1, max(-1, i - 1 - cfg.confirm_window), -1):
                pk, prole, ptext = parsed[j]
                if prole == "owner" and ptext:
                    proposals.append({"text": ptext[:160], "evidence": [pk, key]})
                    break
                if prole == "client" and agreement(ptext) is None:
                    break

        deliverables, stack, access = [], [], []
        seen_stack, seen_access = set(), set()
        for key, text_line in client_lines:
            if WANT_RE.search(text_line) and len(text_line) > 10:
                deliverables.append({"text": text_line[:160], "evidence": [key]})
            for hit in STACK_RE.findall(text_line):
                if hit.lower() not in seen_stack:
                    seen_stack.add(hit.lower())
                    stack.append({"text": hit, "evidence": [key]})
            for hit in ACCESS_RE.findall(text_line):
                kind = next((v for k, v in ACCESS_KIND.items() if hit.lower().startswith(k)),
                            "other")
                if (kind, hit.lower()) not in seen_access:
                    seen_access.add((kind, hit.lower()))
                    access.append({"kind": kind, "name": hit, "evidence": [key]})

        deliverables.extend(proposals)
        goal = None
        if deliverables:
            goal = {"text": deliverables[0]["text"], "evidence": deliverables[0]["evidence"]}
        data = {
            "goal": goal, "deliverables": deliverables, "stack": stack,
            "constraints": [], "assets": [], "access_needed": access,
            "open_questions": ([] if goal else
                               [{"text": "Опиши, пожалуйста, что именно нужно сделать.",
                                 "evidence": [client_lines[0][0]]}] if client_lines else []),
            "out_of_scope": [], "unreadable": [],
            "confidence": 0.8 if goal and access else 0.5,
        }
        return _Resp(json.dumps(data, ensure_ascii=False))


class _Resp:
    def __init__(self, text_body):
        self.content = [type("B", (), {"type": "text", "text": text_body})()]
        self.usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()


# ---------- вывод ----------

async def show_chat(project_id: int) -> list[ChatMessage]:
    async with Session() as s:
        rows = (await s.execute(
            select(ChatMessage)
            .where(ChatMessage.project_id == project_id, ChatMessage.deleted.is_(False))
            .order_by(ChatMessage.created_at, ChatMessage.id))).scalars().all()
    print(f"{LINE}\nПЕРЕПИСКА ({len(rows)} сообщений)\n{LINE}")
    for m in rows:
        media = f"  [{m.media_kind}]" if m.has_media else ""
        body = (m.text or "").replace("\n", " ⏎ ")
        print(f"  [{_msg_key(m)}] {m.sender_role:8s} {body}{media}")
    return rows


def _mark(item: dict) -> str:
    bits = []
    if item.get("origin") == ORIGIN_CONFIRMED:
        bits.append("⚑ ПОДТВЕРЖДЁННОЕ ПРЕДЛОЖЕНИЕ")
    if item.get("missing"):
        bits.append("⧗ не подтверждён последним прогоном")
    return ("   " + "  ".join(bits)) if bits else ""


def _tag(item: dict) -> str:
    """Приоритет и в скольких прогонах из N пункт встретился."""
    bits = []
    if item.get("priority"):
        bits.append(item["priority"])
    if item.get("samples"):
        bits.append(f"прогонов {item['samples']}")
    return f"  [{', '.join(bits)}]" if bits else ""


def show_brief(data: dict) -> None:
    print(f"\n{LINE}\nБРИФ\n{LINE}")
    goal = data.get("goal")
    print(f"  ЦЕЛЬ: {goal['text'] if goal else '— не определена —'}{_mark(goal or {})}")
    if goal:
        print(f"        evidence: {', '.join(goal.get('evidence', []))}")

    # Рамки проекта — сразу после цели. Это не работа, но именно они решают,
    # какое решение вообще уместно, поэтому видеть их надо рано
    for field, label in (("deadline", "СРОК"), ("budget", "БЮДЖЕТ")):
        item = data.get(field)
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            extra = ""
            if field == "deadline" and item.get("date"):
                extra = f"  (дата: {item['date']})"
            if field == "budget" and item.get("amount"):
                extra = f"  ({item['amount']} {item.get('currency') or ''})".rstrip() + ")"
                extra = f"  ({item['amount']} {item.get('currency') or ''})"
            print(f"  {label}: {item['text']}{extra}{_mark(item)}")
            print(f"        evidence: {', '.join(item.get('evidence', []))}")

    for field in LIST_FIELDS:
        items = data.get(field) or []
        if not items:
            continue
        print(f"\n  {field.upper()} ({len(items)}):")
        for item in items:
            label = item.get("text") or f"{item.get('kind')}: {item.get('name')}"
            print(f"    • {label}{_tag(item)}{_mark(item)}")
            if item.get("priority_reason"):
                print(f"      модальность: {item['priority_reason']}")
            print(f"      evidence: {', '.join(item.get('evidence', []))}")
    if data.get("unreadable"):
        print(f"\n  НЕ ПРОЧИТАНО ({len(data['unreadable'])}):")
        for u in data["unreadable"]:
            print(f"    • сообщение {u.get('message_id')} — {u.get('kind')}")
    print(f"\n  CONFIDENCE: {data.get('confidence')}  (порог {cfg.brief_min_confidence})")


def show_confirmed(data: dict) -> None:
    """Самое рискованное место схемы — показываем отдельно и заметно.

    Здесь требование держится не на словах клиента, а на нашей трактовке его
    «да». Ошибка тут означает пункт ТЗ, которого клиент не просил.
    """
    rows = []
    goal = data.get("goal")
    if isinstance(goal, dict) and goal.get("origin") == ORIGIN_CONFIRMED:
        rows.append(("goal", goal))
    for field in LIST_FIELDS:
        for item in data.get(field) or []:
            if isinstance(item, dict) and item.get("origin") == ORIGIN_CONFIRMED:
                rows.append((field, item))

    print(f"\n{LINE}\n⚑ ПОДТВЕРЖДЁННЫЕ ПРЕДЛОЖЕНИЯ ({len(rows)}) — ПРОВЕРЬ ГЛАЗАМИ\n{LINE}")
    if not rows:
        print("  (нет: всё в брифе сказано клиентом напрямую)")
        return
    print("  Эти пункты клиент сам не формулировал: их предложил ты, а он согласился.")
    print("  Смотри, действительно ли согласие относится именно к этому предложению.\n")
    for field, item in rows:
        label = item.get("text") or f"{item.get('kind')}: {item.get('name')}"
        flag = " [ОТКАЗ]" if item.get("rejected") else ""
        print(f"    • [{field}]{flag} {label}")
        for ref in item.get("evidence", []):
            print(f"        ← {ref}")


def show_dropped(project: Project) -> None:
    meta = (project.brief or {}).get("_meta") or {}
    dropped = meta.get("dropped") or []
    print(f"\n{LINE}\nВЫБРОШЕНО КОДОМ ({len(dropped)})\n{LINE}")
    for d in dropped:
        print(f"  ✗ {d}")
    if not dropped:
        print("  (ничего — все пункты подтверждены словами клиента)")


VERDICT_MARK = {"covered": "✓", "partial": "~", "missing": "✗"}


def show_missing(data: dict) -> None:
    """Пункты, которых не было в последнем прогоне. Мы их не выбрасываем."""
    rows = missing_items(data)
    print(f"\n{LINE}\n⧗ НЕ ПОДТВЕРЖДЕНЫ ПОСЛЕДНИМ ПРОГОНОМ ({len(rows)})\n{LINE}")
    if not rows:
        print("  (нет: всё, что было, подтвердилось снова)")
        return
    print("  Модель не вернула их в этот раз. Пункт остаётся в брифе: молча")
    print("  терять требование нельзя, а убрать его может только отказ клиента")
    print("  или ты сам.\n")
    for field, item in rows:
        label = item.get("text") or f"{item.get('kind')}: {item.get('name')}"
        seen = item.get("seen_count", "?")
        print(f"    • [{field}] {label}")
        print(f"        видели раз: {seen}, впервые: {str(item.get('first_seen'))[:19]}")


def show_coverage(results: list[dict]) -> int:
    print(f"\n{LINE}\nПОКРЫТИЕ ЭТАЛОНА ({len(results)} требований)\n{LINE}")
    if not results:
        print("  (эталона нет — создать черновик: --draft)")
        return 0
    bad = 0
    for r in results:
        verdict = str(r.get("verdict", "partial"))
        mark = VERDICT_MARK.get(verdict, "?")
        if verdict != "covered":
            bad += 1
        print(f"  {mark} {r.get('requirement')}")
        if r.get("matched"):
            print(f"      → {r['matched']}")
        if r.get("note") and verdict != "covered":
            print(f"      причина: {r['note']}")
    covered = len(results) - bad
    print(f"\n  покрыто {covered} из {len(results)}")
    return bad


def draft_requirements(data: dict) -> str:
    """Черновик эталона из текущего брифа — дальше правится руками."""
    lines = ["# Требования к этому чату. Одно в строке, свободная форма.",
             "# Это ЭТАЛОН: что обязано оказаться в ТЗ, а не что модель уже нашла.",
             "# Правь руками — черновик собран из прогона и может быть неполным.",
             ""]
    goal = data.get("goal")
    if isinstance(goal, dict) and goal.get("text"):
        lines.append(goal["text"])
    for field in ("deliverables", "constraints", "access_needed"):
        for item in data.get(field) or []:
            if isinstance(item, dict):
                lines.append(item.get("text") or f"{item.get('kind')}: {item.get('name')}")
    return "\n".join(lines) + "\n"


async def _restore(project_id: int, brief_data, ready: bool, status: str) -> None:
    """Вернуть проект в состояние до прогона."""
    async with Session() as s:
        p = await s.get(Project, project_id)
        if p is not None:
            p.brief = brief_data
            p.brief_ready = ready
            p.status = status
            await s.commit()



async def show_spend() -> None:
    """Две цифры РАЗДЕЛЬНО: деньги и подписка. Это разные ресурсы."""
    both = await consumed_today()
    print(f"\n{LINE}\nРАСХОД ЗА СУТКИ\n{LINE}")
    print(f"  реальные деньги (API):  ${both['api_usd']:.2f}   "
          f"вызовов {int(both['api_calls'])}")
    print(f"  подписка (CLI):         ~${both['cli_usd_est']:.2f} оценочно, "
          f"вызовов {int(both['cli_calls'])}, {both['cli_seconds'] / 60:.1f} мин")
    print("  суточный бюджет считает ТОЛЬКО первую строку: подписка деньгами")
    print("  не тратится, и останавливать работу из-за неё было бы неверно")


async def run(project_id: int, stub: bool, draft: bool) -> int:
    await init_db()
    async with Session() as s:
        project = await s.get(Project, project_id)
    if project is None:
        print(f"нет проекта {project_id}", file=sys.stderr)
        return 2

    print(f"\nПРОЕКТ {project.id}: {project.title} (клиент: {project.client})")
    rows = await show_chat(project.id)
    if not rows:
        print("  переписки нет — брифу не из чего собираться")
        return 1

    if stub:
        print("\n[режим --stub: модель НЕ вызывается, работает офлайн-заглушка]")
        brief = Brief(client=StubModel())
    elif not anthropic_key():
        print(missing_secret_message("ANTHROPIC_API_KEY"), file=sys.stderr)
        print("Либо задай ключ, либо гоняй с --stub", file=sys.stderr)
        return 2
    else:
        print(f"\n[живая модель {cfg.brief_model}, ключ взят из: "
              f"{anthropic_key_source()}, прогонов: {cfg.brief_samples}]")
        brief = Brief()

    # Для eval всегда полный пересбор: смотреть надо на весь чат.
    # Прежнее состояние запоминаем — неудачный прогон обязан его вернуть.
    # Иначе диагностика уничтожает ровно то, что диагностирует: один упавший
    # прогон уже стирал собранный бриф проекта 8 подчистую.
    async with Session() as s:
        p = await s.get(Project, project_id)
        saved = (p.brief, p.brief_ready, p.status)
        p.brief = {}
        await s.commit()
        project = await s.get(Project, project_id)

    try:
        data = await brief.build(project)
    except LimitReached as e:
        await _restore(project_id, *saved)
        print(f"\nКВОТА ПОДПИСКИ ИСЧЕРПАНА: {e}\n"
              f"Бриф не тронут, попытка не засчитана — повтори, когда "
              f"откроется окно.", file=sys.stderr)
        return 3
    except LLMError as e:
        await _restore(project_id, *saved)
        print(f"\nМОДЕЛЬ НЕДОСТУПНА: {e}\nПрежний бриф возвращён на место.",
              file=sys.stderr)
        return 2
    except BaseException:
        await _restore(project_id, *saved)
        print("\nпрогон прерван — прежний бриф возвращён на место", file=sys.stderr)
        raise
    if data is None:
        await _restore(project_id, *saved)
        print("\nбриф не собран — смотри лог выше. Прежний бриф возвращён на место")
        return 1

    show_brief(data)
    show_confirmed(data)
    show_missing(data)
    async with Session() as s:
        show_dropped(await s.get(Project, project_id))
    await show_spend()

    fixture = FIXTURES / f"{project_id}.requirements.txt"
    if draft:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        fixture.write_text(draft_requirements(data), encoding="utf-8")
        print(f"\nчерновик эталона: {fixture.relative_to(ROOT)} — поправь руками")
        print()
        return 0

    if not fixture.exists():
        print(f"\nэталона нет: {fixture.relative_to(ROOT)} (создать черновик: --draft)")
        print()
        return 0

    requirements = parse_requirements(fixture.read_text(encoding="utf-8"))
    if stub:
        print(f"\nэталон есть ({len(requirements)} требований), но сверка требует "
              f"модели — в режиме --stub пропущена")
        print()
        return 0

    try:
        results = await check_coverage(requirements, data)
    except Exception as e:
        # бриф уже собран и показан выше — терять его из-за сверки нельзя
        print(f"\nсверка с эталоном не выполнена: {e}", file=sys.stderr)
        print()
        return 0
    bad = show_coverage(results)
    print()
    return 1 if bad else 0


async def show_projects() -> int:
    await init_db()
    async with Session() as s:
        rows = (await s.execute(
            select(Project.id, Project.title, Project.client, func.count(ChatMessage.id))
            .outerjoin(ChatMessage, ChatMessage.project_id == Project.id)
            .group_by(Project.id).order_by(Project.id))).all()
    print("id   сообщений  проект")
    for pid, title, client, n in rows:
        print(f"{pid:<4} {n:<10} {title} ({client})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="прогон brief.py по реальному чату")
    ap.add_argument("--project", type=int)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--stub", action="store_true", help="без вызова модели")
    ap.add_argument("--draft", action="store_true",
                    help="записать черновик эталонного списка требований")
    ap.add_argument("--samples", type=int, default=None,
                    help="сколько раз собрать бриф (перекрывает BRIEF_SAMPLES)")
    args = ap.parse_args()
    if args.samples is not None:
        cfg.brief_samples = args.samples
    if args.list or not args.project:
        return asyncio.run(show_projects())
    return asyncio.run(run(args.project, args.stub, args.draft))


if __name__ == "__main__":
    raise SystemExit(main())
