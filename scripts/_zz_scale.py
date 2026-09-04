"""ВРЕМЕННЫЙ скрипт: масштаб находки по живой базе. Удалить."""
import json, sqlite3, sys
sys.path.insert(0, ".")
from autopilot import checks, evidence

con = sqlite3.connect("autopilot.db")
rows = con.execute("select id, project_id, status, acceptance from tasks").fetchall()

всего = 0
могут_сами = 0        # есть хоть один детерминированный непустышечный критерий
теряют = 0            # ... и ВСЕ такие критерии называют файл
с_именем = 0          # задач, где есть хоть один критерий с явным файлом
имена = {}
for tid, pid, st, acc in rows:
    try:
        a = json.loads(acc or "[]")
    except Exception:
        continue
    if not isinstance(a, list):
        continue
    всего += 1
    твёрдые = []
    for c in a:
        if not isinstance(c, dict):
            continue
        s = checks.spec(str(c.get("type") or ""))
        if s is None or not s.deterministic:
            continue
        if not checks.is_runnable(c):
            continue
        if checks.suspicious(c):
            continue
        твёрдые.append(c)
    named = [c for c in твёрдые if evidence.files_of(c)]
    if named:
        с_именем += 1
        for c in named:
            for f in evidence.files_of(c):
                имена[f] = имена.get(f, 0) + 1
    if твёрдые:
        могут_сами += 1
        if len(named) == len(твёрдые):
            теряют += 1

print("задач всего:", всего)
print("могут закрыться сами (есть твёрдый критерий):", могут_сами)
print("из них все твёрдые критерии называют файл -> потеряют способность:", теряют)
print("задач, где есть критерий с явным именем файла:", с_именем)
топ = sorted(имена.items(), key=lambda kv: -kv[1])[:15]
print("частые файлы:", топ)
