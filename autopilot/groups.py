"""Опознание проекта по названию группового чата.

Менеджер называет группы по шаблону из GROUP_NAME_TEMPLATE, например
«Qorsa • Айгерим • интернет-магазин». Мы разбираем название на части и
сравниваем с колонками «Клиент» и «Проект» из таблицы.

Сравнение нечёткое: менеджер напишет «интернет магазин» вместо
«интернет-магазин» и не подумает, что что-то сломал. Но порог высокий:
привязать чат не к тому проекту хуже, чем спросить.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

from sqlalchemy import select

from .config import cfg
from .db import Project, Session

log = logging.getLogger("groups")


def normalize(text: str) -> str:
    """Регистр, ё/е, дефисы и лишние пробелы значения не имеют."""
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[-–—_/\\.,:;!?()«»\"']+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # вхождение целиком — сильный сигнал: «магазин» внутри «интернет магазин»
    if a in b or b in a:
        return max(0.9, SequenceMatcher(None, a, b).ratio())
    return SequenceMatcher(None, a, b).ratio()


def parse_title(title: str, template: str | None = None) -> tuple[str, str] | None:
    """«Qorsa • Айгерим • интернет-магазин» -> ("Айгерим", "интернет-магазин").

    Шаблон превращается в регулярку, а не парсится позиционно: менеджер может
    поменять разделитель, и лучше честно не опознать, чем опознать наугад.
    """
    tpl = template or cfg.group_name_template
    if "{client}" not in tpl or "{project}" not in tpl:
        return None
    pattern = re.escape(tpl).replace(r"\{client\}", "(?P<client>.+?)") \
                            .replace(r"\{project\}", "(?P<project>.+)")
    m = re.match(pattern, (title or "").strip(), re.IGNORECASE)
    if m:
        return m.group("client").strip(), m.group("project").strip()

    # запасной разбор: тот же разделитель, но лишние/недостающие части
    sep = re.sub(r"\{client\}|\{project\}|Qorsa|\w", "", tpl).strip()
    sep = sep[0] if sep else "•"
    parts = [p.strip() for p in (title or "").split(sep) if p.strip()]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return None


async def match_project(title: str) -> tuple[int | None, float, str]:
    """(project_id, уверенность, пояснение). project_id только при уверенном совпадении."""
    parsed = parse_title(title)
    if not parsed:
        return None, 0.0, f"название {title!r} не по шаблону {cfg.group_name_template!r}"
    client, project_name = parsed

    async with Session() as s:
        projects = (await s.execute(
            select(Project).where(Project.status.notin_(("done",))))).scalars().all()
    if not projects:
        return None, 0.0, "в базе нет открытых проектов"

    scored = []
    for p in projects:
        # обе части важны: одинаковых клиентов у разных проектов сколько угодно
        score = 0.4 * similarity(client, p.client) + 0.6 * similarity(project_name, p.title)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < cfg.group_match_threshold:
        return None, best_score, (f"лучшее совпадение {best.title!r} = {best_score:.2f}, "
                                  f"порог {cfg.group_match_threshold}")
    if best_score - runner_up < 0.1:
        # два похожих проекта — угадывать нельзя
        return None, best_score, (f"неоднозначно: {best.title!r}={best_score:.2f} против "
                                  f"{scored[1][1].title!r}={runner_up:.2f}")
    return best.id, best_score, f"совпало с {best.title!r} ({best_score:.2f})"
