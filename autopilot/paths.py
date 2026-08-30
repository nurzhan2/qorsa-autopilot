"""Какие файлы трогает задача — по её критериям приёмки.

Один и тот же ответ нужен в двух местах, и это не совпадение:

* планировщику — чтобы не пустить в работу параллельно две задачи, которые
  правят один каталог (они затрут работу друг друга молча);
* регрессионной проверке — чтобы понять, чью работу эта задача могла сломать.

Пути берутся ТОЛЬКО из критериев, то есть те, что назвал планировщик. Файл,
который агент тронет по ходу, но который в критериях не упомянут, сюда
не попадает. Это осознанная граница: список известного лучше догадки,
а гадать по названию задачи мы не будем.
"""
from __future__ import annotations

import re

# По первым двум сегментам: `backend/app`, `client_app/lib`. Один сегмент
# слишком грубо (весь `backend` — это половина проекта), три — слишком точно
# (задачи по соседним файлам одного модуля отлично мешают друг другу).
DEPTH = 2

_SPLIT = re.compile(r"[\s&|;'\"`]+")


def of(task) -> set[str]:
    """Каталоги, которые задача, судя по её критериям, трогает."""
    out: set[str] = set()
    for check in (getattr(task, "acceptance", None) or []):
        if not isinstance(check, dict):
            continue
        for key in ("path", "cmd"):
            for token in _SPLIT.split(str(check.get(key) or "")):
                token = token.strip().lstrip("./")
                if "/" not in token:
                    continue
                head = token.split("::")[0]
                parts = [p for p in head.split("/") if p and p != ".."]
                if len(parts) >= DEPTH:
                    out.add("/".join(parts[:DEPTH]))
                elif parts:
                    out.add(parts[0])
    return out


def overlap(a, b) -> bool:
    """Пересекаются ли задачи по файлам. Пустой список путей — не пересечение:
    про такую задачу мы попросту ничего не знаем, и запрещать её нельзя."""
    mine, theirs = of(a), of(b)
    return bool(mine and theirs and (mine & theirs))
