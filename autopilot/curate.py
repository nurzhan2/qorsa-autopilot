"""Правки ТЗ руками: добавить требование, снять требование, зафиксировать стек.

**План правится через ТЗ, а не мимо него.** Это главное правило модуля, и оно
не про красоту: задачи выводятся из брифа, а перепланирование сверяет каждую
задачу с пунктом ТЗ (`deliverable_ref`). Задача, дописанная в план руками,
не найдёт своего пункта и на следующем прогоне планировщика уедет в `orphaned`
— то есть правка молча откатится. Поэтому дописывать надо требование, а план
пересобирать.

Ручной пункт — тот самый `curated` из brief.py, у него три свойства, и все
три здесь нужны:

* **evidence не требуется** — правило про ссылки существует против выдумок
  МОДЕЛИ, а владелец вправе зафиксировать то, чего клиент не говорил;
* **не помечается `missing`** — прогон модели его не подтверждает и не должен;
* **переживает полный пересбор** — `restore_curated()` возвращает его на место.

Снятое требование не удаляется, а переезжает в `out_of_scope` с пометкой,
что снял его владелец. Тем же путём, что и отказ клиента: в брифе не должно
быть дыр, по которым потом не понять, требование забыли или отменили.
"""
from __future__ import annotations

import datetime as dt
import logging

from .brief import (LIST_FIELDS, PRIORITIES, item_text, same_item)
from .db import Project, Session, utcnow

log = logging.getLogger("curate")

OWNER_REASON = "снято владельцем"


def _brief_of(project: Project) -> dict:
    payload = project.brief if isinstance(project.brief, dict) else {}
    return dict(payload.get("brief") or {})


def _save(project: Project, brief: dict) -> dict:
    payload = dict(project.brief or {}) if isinstance(project.brief, dict) else {}
    payload["brief"] = brief
    meta = dict(payload.get("_meta") or {})
    meta["updated_at"] = utcnow().isoformat()
    payload["_meta"] = meta
    return payload


def make_item(text: str, *, priority: str = "must") -> dict:
    """Ручной пункт ТЗ. `evidence` пустой намеренно — см. модульную справку."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "text": text.strip(),
        "evidence": [],
        "curated": True,
        "origin": "owner",
        "priority": priority if priority in PRIORITIES else "must",
        "priority_reason": "поставлено владельцем вручную",
        "first_seen": now,
        "last_seen": now,
        "seen_count": 1,
    }


async def add(project_id: int, text: str, *, field: str = "deliverables",
              priority: str = "must") -> dict:
    """Дописать требование в ТЗ руками.

    Похожий пункт не дублируется: сравнение то же нечёткое, что у брифа.
    Если пункт уже есть, он становится `curated` — то есть решение владельца
    перекрывает формулировку модели, а не соседствует с ней.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("пустое требование")
    if field not in LIST_FIELDS:
        raise ValueError(f"поле {field!r} не из {LIST_FIELDS}")

    async with Session() as s:
        project = await s.get(Project, int(project_id))
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        brief = _brief_of(project)
        rows = [dict(i) for i in (brief.get(field) or []) if isinstance(i, dict)]

        fresh = make_item(text, priority=priority)
        matched = next((r for r in rows if same_item(r, fresh)), None)
        if matched is not None:
            # пункт уже есть — делаем его ручным, а не заводим второй
            matched.update(curated=True, origin="owner", text=text,
                           priority=fresh["priority"],
                           priority_reason=fresh["priority_reason"])
            matched.pop("missing", None)
            action = "существующий пункт помечен ручным"
        else:
            rows.append(fresh)
            action = "требование добавлено"

        brief[field] = rows
        project.brief = _save(project, brief)
        project.updated_at = utcnow()
        await s.commit()

    log.info("проект %s: %s — %r", project_id, action, text[:80])
    return {"action": action, "field": field, "text": text}


async def drop(project_id: int, text: str, *, field: str = "deliverables",
               reason: str = OWNER_REASON) -> dict:
    """Снять требование: убрать из поля и записать в `out_of_scope`.

    Не удаляем молча. В брифе не должно быть дыр, по которым через месяц
    не понять, требование забыли или отменили осознанно.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("нечего снимать")

    async with Session() as s:
        project = await s.get(Project, int(project_id))
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        brief = _brief_of(project)
        rows = [dict(i) for i in (brief.get(field) or []) if isinstance(i, dict)]
        probe = {"text": text}
        keep = [r for r in rows if not (item_text(r) == text or same_item(r, probe))]
        if len(keep) == len(rows):
            raise ValueError(f"пункт {text!r} в поле {field} не найден")

        brief[field] = keep
        out = [dict(i) for i in (brief.get("out_of_scope") or []) if isinstance(i, dict)]
        out.append({"text": text, "evidence": [], "curated": True,
                    "origin": "owner", "rejected": True, "reason": reason})
        brief["out_of_scope"] = out

        project.brief = _save(project, brief)
        project.updated_at = utcnow()
        await s.commit()

    log.info("проект %s: снято требование %r (%s)", project_id, text[:80], reason)
    return {"action": "снято", "text": text, "reason": reason}


async def restore(project_id: int, text: str, *, field: str = "deliverables") -> dict:
    """Вернуть снятое требование обратно в объём работ."""
    text = (text or "").strip()
    async with Session() as s:
        project = await s.get(Project, int(project_id))
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        brief = _brief_of(project)
        out = [dict(i) for i in (brief.get("out_of_scope") or []) if isinstance(i, dict)]
        keep = [r for r in out if item_text(r) != text]
        if len(keep) == len(out):
            raise ValueError(f"{text!r} нет среди снятых")
        brief["out_of_scope"] = keep
        rows = [dict(i) for i in (brief.get(field) or []) if isinstance(i, dict)]
        rows.append(make_item(text))
        brief[field] = rows
        project.brief = _save(project, brief)
        project.updated_at = utcnow()
        await s.commit()
    log.info("проект %s: требование возвращено — %r", project_id, text[:80])
    return {"action": "возвращено", "text": text}


async def answer_question(project_id: int, question: str, answer: str,
                          *, as_requirement: bool = True) -> dict:
    """Владелец ответил на блокирующий вопрос ТЗ сам.

    Такой вопрос держит весь заказ: `brief_ready` не станет истиной, пока он
    открыт, а полоса build не выдаст ни одного слота. Ответить на него было
    нечем — вопросы приходят от модели и заменяются целиком на следующем
    прогоне, то есть закрыть его можно было только новой репликой в чате.

    Делаем три вещи, и все три нужны:

    1. **пишем ответ в переписку** как реплику владельца. Без этого следующий
       полный пересбор задаст тот же вопрос снова: модель читает сообщения,
       а не наши структуры;
    2. **убираем вопрос из `open_questions`** — здесь и сейчас, не дожидаясь
       прогона модели. Иначе заказ стоит до следующего вызова, а вызов может
       и не пройти;
    3. **пересчитываем `brief_ready` тут же** по тем же правилам, что
       и `Brief._persist`: цель есть, блокирующих вопросов нет, уверенности
       хватает. Так снятие последнего вопроса разблокирует заказ сразу.

    Ответ — это РЕШЕНИЕ ВЛАДЕЛЬЦА, а не слова клиента, и записывается он
    именно так: `curated`-пунктом с `origin="owner"`. Выдавать его за
    требование клиента нельзя — на этом держится всё правило evidence.
    """
    from .brief import blocking_questions
    from .config import cfg
    from .db import ChatMessage
    from .intake import TRANSPORT, chat_id_for
    from .roles import OWNER
    from .secrets_scan import scrub

    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question or not answer:
        raise ValueError("нужны и вопрос, и ответ")

    async with Session() as s:
        project = await s.get(Project, int(project_id))
        if project is None:
            raise ValueError(f"проекта {project_id} нет")
        brief = _brief_of(project)
        questions = [q for q in (brief.get("open_questions") or [])
                     if isinstance(q, dict)]
        keep = [q for q in questions
                if str(q.get("text") or "").strip() != question]
        if len(keep) == len(questions):
            raise ValueError("такого вопроса в ТЗ нет")
        brief["open_questions"] = keep

        if as_requirement:
            rows = [dict(i) for i in (brief.get("constraints") or [])
                    if isinstance(i, dict)]
            item = make_item(f"{question} — {answer}")
            if not any(same_item(r, item) for r in rows):
                rows.append(item)
            brief["constraints"] = rows

        # Ответ — в переписку, иначе следующий пересбор спросит то же самое
        text, _names = scrub(f"РЕШЕНИЕ ВЛАДЕЛЬЦА. {question}\nОтвет: {answer}",
                             project_id=project_id, chat_id=chat_id_for(project_id))
        s.add(ChatMessage(
            transport=TRANSPORT, chat_id=chat_id_for(project_id),
            tg_message_id=f"owner-{utcnow().strftime('%Y%m%d%H%M%S%f')}",
            project_id=project_id, direction="out", sender_role=OWNER,
            sender_id="owner", text=text,
            raw_json={"owner_decision": True, "question": question},
            created_at=utcnow()))

        # Те же правила, что у Brief._persist — второго мнения тут быть не должно
        ready = (bool(brief.get("goal"))
                 and not blocking_questions(brief)
                 and float(brief.get("confidence") or 0) >= cfg.brief_min_confidence)
        project.brief = _save(project, brief)
        project.brief_ready = ready
        project.updated_at = utcnow()
        await s.commit()

    log.warning("проект %s: владелец ответил на вопрос ТЗ сам — %r; "
                "осталось блокирующих: %s, ТЗ собрано: %s",
                project_id, question[:70],
                len([q for q in keep if q.get("blocking")]), ready)
    return {"action": "вопрос закрыт", "brief_ready": ready,
            "left": len([q for q in keep if q.get("blocking")])}


def listing(project: Project) -> dict:
    """Что показывать в окне правок ТЗ."""
    brief = _brief_of(project)

    def rows(field: str) -> list[dict]:
        out = []
        for i in (brief.get(field) or []):
            if not isinstance(i, dict):
                continue
            out.append({
                "text": item_text(i),
                "curated": bool(i.get("curated")),
                "missing": bool(i.get("missing")),
                "priority": i.get("priority") or "",
                "origin": i.get("origin") or "",
                "reason": i.get("reason") or "",
            })
        return out

    questions = [{
        "text": str(q.get("text") or "").strip(),
        "blocking": bool(q.get("blocking")),
    } for q in (brief.get("open_questions") or []) if isinstance(q, dict)]

    return {
        "deliverables": rows("deliverables"),
        "stack": rows("stack"),
        "constraints": rows("constraints"),
        "out_of_scope": rows("out_of_scope"),
        # блокирующие впереди: именно они держат заказ
        "questions": sorted(questions, key=lambda q: not q["blocking"]),
    }
