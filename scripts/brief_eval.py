"""Прогон brief.py по реальным чатам с показом результата рядом с перепиской.

Урок фазы 2: баги детектора вылезли только на живых данных, синтетика их
не показала. Поэтому бриф надо смотреть глазами, а не только тестами.

    python scripts/brief_eval.py --list                 # что есть в базе
    python scripts/brief_eval.py --project 1            # прогнать и показать
    python scripts/brief_eval.py --project 1 --stub     # без вызова модели
    python scripts/brief_eval.py --project 1 --save     # закрепить как эталон
    python scripts/brief_eval.py --project 1 --compare  # сверить с эталоном

`--stub` подставляет вместо модели грубый детерминированный экстрактор.
Он НЕ показывает качество модели — он прогоняет весь тракт (evidence, роли,
запрет на деньги, доступы, вопросы) там, где ключа нет.

Эталоны лежат в tests/fixtures/briefs/<project>.json.
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
                             _msg_key, agreement)
from autopilot.config import cfg                                       # noqa: E402
from autopilot.db import ChatMessage, Project, Session, init_db        # noqa: E402
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
    return "   ⚑ ПОДТВЕРЖДЁННОЕ ПРЕДЛОЖЕНИЕ" if item.get("origin") == ORIGIN_CONFIRMED else ""


def show_brief(data: dict) -> None:
    print(f"\n{LINE}\nБРИФ\n{LINE}")
    goal = data.get("goal")
    print(f"  ЦЕЛЬ: {goal['text'] if goal else '— не определена —'}{_mark(goal or {})}")
    if goal:
        print(f"        evidence: {', '.join(goal.get('evidence', []))}")
    for field in LIST_FIELDS:
        items = data.get(field) or []
        if not items:
            continue
        print(f"\n  {field.upper()} ({len(items)}):")
        for item in items:
            label = item.get("text") or f"{item.get('kind')}: {item.get('name')}"
            print(f"    • {label}{_mark(item)}")
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


def compare(current: dict, golden: dict) -> int:
    print(f"\n{LINE}\nСРАВНЕНИЕ С ЭТАЛОНОМ\n{LINE}")
    diffs = 0
    for field in ("goal",) + LIST_FIELDS:
        if field == "goal":
            a = (current.get("goal") or {}).get("text")
            b = (golden.get("goal") or {}).get("text")
            if a != b:
                diffs += 1
                print(f"  ≠ goal:\n      было:  {b}\n      стало: {a}")
            continue
        a = {(i.get("text") or i.get("name") or "").strip().lower()
             for i in (current.get(field) or [])}
        b = {(i.get("text") or i.get("name") or "").strip().lower()
             for i in (golden.get(field) or [])}
        for lost in sorted(b - a):
            diffs += 1
            print(f"  − {field}: пропало «{lost}»")
        for added in sorted(a - b):
            diffs += 1
            print(f"  + {field}: появилось «{added}»")
    if not diffs:
        print("  совпадает с эталоном полностью")
    return diffs


async def run(project_id: int, stub: bool, save: bool, do_compare: bool) -> int:
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
              f"{anthropic_key_source()}]")
        brief = Brief()

    # для eval всегда полный пересбор: смотреть надо на весь чат
    async with Session() as s:
        p = await s.get(Project, project_id)
        p.brief = {}
        await s.commit()
        project = await s.get(Project, project_id)

    data = await brief.build(project)
    if data is None:
        print("\nбриф не собран — смотри лог выше")
        return 1

    show_brief(data)
    show_confirmed(data)
    async with Session() as s:
        show_dropped(await s.get(Project, project_id))

    fixture = FIXTURES / f"{project_id}.json"
    if save:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nэталон сохранён: {fixture.relative_to(ROOT)}")
    elif do_compare or fixture.exists():
        if not fixture.exists():
            print(f"\nэталона нет: {fixture.relative_to(ROOT)} (создать: --save)")
        else:
            golden = json.loads(fixture.read_text(encoding="utf-8"))
            if compare(data, golden):
                return 1
    print()
    return 0


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
    ap.add_argument("--save", action="store_true", help="сохранить результат как эталон")
    ap.add_argument("--compare", action="store_true", help="сверить с эталоном")
    args = ap.parse_args()
    if args.list or not args.project:
        return asyncio.run(show_projects())
    return asyncio.run(run(args.project, args.stub, args.save, args.compare))


if __name__ == "__main__":
    raise SystemExit(main())
