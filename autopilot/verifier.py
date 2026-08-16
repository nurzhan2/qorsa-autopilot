"""Три уровня проверки. Судья — отдельная модель, НЕ тот агент, что писал код."""
from __future__ import annotations

import asyncio
import base64
import dataclasses as dc
import json
import logging
import os
import time
from pathlib import Path

import httpx

from .checks import (DEFAULT_TIMEOUT_SEC, HUMAN_ONLY, REGISTRY, VIEWPORTS,
                     is_runnable, spec)
from .checks import suspicious as suspicious_reason
from .config import cfg
from . import llm
from .llm import LLMError
from .db import Project, Session, Task, close_run, open_run
from .vault import anthropic_key, missing_secret_message

log = logging.getLogger("verify")

JUDGE_PROMPT = """Ты принимаешь ОДНУ задачу по ОДНОМУ критерию.

Ты отвечаешь ровно на один вопрос: ВЫПОЛНЕН ЛИ ЭТОТ КРИТЕРИЙ.
Не «хорош ли код», не «безопасен ли он», не «полон ли проект», не «есть ли
тесты». Всё это может быть важно — но это не тот вопрос, который тебе задали.

КРИТЕРИЙ — единственное, что решает вердикт:
{criteria}

Диф изменений:
{diff}

Отчёт исполнителя (проверяй по дифу, а не по отчёту):
{report}

Правила:
1. PASS — если критерий выполнен. FAIL — если нарушена конкретная часть
   критерия. Других оснований для FAIL нет.
2. У каждого дефекта поле `criterion_part` — ДОСЛОВНАЯ выдержка из текста
   критерия выше, та самая часть, которая не выполнена. Дефект без такой
   выдержки в вердикт не попадёт: код его отбросит.
   Выдержка — это НАРУШЕННОЕ ТРЕБОВАНИЕ, а не упоминание объекта. «Обработчик
   вебхука» — объект; «проверка подписи» — требование. Сослаться на объект,
   чтобы протащить постороннее замечание, не выйдет: код сверит ещё и то,
   об одном ли говорят критерий и дефект.
3. Всё остальное, что ты заметил в дифе — отсутствие тестов и обработки
   ошибок, идемпотентность, стиль, безопасность, незакрытые края — пиши
   в `observations`. Это читает владелец, из этого выходят следующие задачи,
   но на вердикт это НЕ ВЛИЯЕТ.
4. Требует ли критерий чего-то — решает текст критерия, а не твои
   представления о том, как надо писать такой код.
5. Диф пуст или в нём нечего проверять — это FAIL с выдержкой из критерия:
   невыполненный критерий, а не отдельный разговор.

Верни СТРОГО JSON без markdown:
{{"verdict": "PASS" | "FAIL",
  "defects": [{{"defect": "что именно не так",
               "criterion_part": "дословная выдержка из критерия"}}],
  "observations": ["замечание вне критерия", ...]}}
"""

SHELL_TIMEOUT_SEC = 600

# Ответ судьи стал длиннее: к вердикту добавились выдержки из критерия
# и наблюдения. 1500 токенов начали обрываться на середине JSON, а
# оборванный ответ — это FAIL «вердикт не разобран», то есть ровно тот
# ложный провал, ради которого всё это переписывалось.
JUDGE_MAX_TOKENS = 3000


@dc.dataclass(frozen=True)
class Verdict:
    """Итог приёмки. Важно не только «прошло», но и ЧЕМ прошло.

    `screenshot` и `llm` мы сами называем подсказкой: модель посмотрела на
    картинку и сказала, что похоже на описанное. До этой правки такой вердикт
    попадал в статус задачи неотличимо от зелёного теста — то есть оценка
    модели по скриншоту приравнивалась к `npm run build` с кодом возврата 0.
    Это неправда, и это ровно тот молчаливый пропуск, ради которого затевалась
    вся фаза: задача выглядела принятой, хотя ничего детерминированного её
    не проверяло.

    Поэтому счётчики раздельные, а решение о статусе принимает вызывающий код
    по двум свойствам ниже. Сам верификатор статусов не ставит: он сообщает
    факты, последнее слово — за кодом, который эти факты читает.
    """

    ok: bool
    defects: list[str]
    # сколько проверок РЕАЛЬНО исполнено, по видам вердикта
    deterministic: int = 0      # shell / http / file_exists / dom — судит код
    assisted: int = 0           # screenshot / llm — судит модель
    # детерминированные, но помеченные checks.suspicious(): исполнены и,
    # возможно, пройдены — однако доказательством не считаются
    suspicious: int = 0
    # Замечания судьи ВНЕ критерия: на вердикт не влияют, но и не пропадают.
    # Судья видит диф целиком и замечает настоящие вещи — отсутствие тестов,
    # необработанные ошибки. Приёмка это не решает, а следующая задача может
    observations: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        """Прошла так, что машине можно верить: есть хоть одна проверка,
        вердикт которой вынес код, а не модель, и которая при этом что-то
        доказывает. Только это закрывает задачу автоматически."""
        return self.ok and self.deterministic > 0

    @property
    def unproven(self) -> bool:
        """Прошла, но доказательства нет.

        Два способа сюда попасть, и оба одинаково пусты:
        * все проверки assisted — вердикт вынесла модель;
        * все детерминированные проверки подозрительные — они пройдут и
          на невыполненной задаче, то есть не проверяют ничего.

        Не провал и не приёмка: задача уходит владельцу на подтверждение.
        """
        return self.ok and self.deterministic == 0

    def why_unproven(self) -> str:
        """Чем именно приёмка оказалась пустой — владельцу это решать."""
        parts = []
        if self.assisted:
            parts.append(f"проверок с моделью: {self.assisted}")
        if self.suspicious:
            parts.append(f"критериев, которые пройдут и без этой задачи: "
                         f"{self.suspicious}")
        if not parts:
            parts.append("исполнимых проверок нет")
        return "; ".join(parts)

    def summary(self) -> str:
        out = (f"детерминированных проверок: {self.deterministic}, "
               f"с моделью: {self.assisted}")
        if self.suspicious:
            out += f", пустышек: {self.suspicious}"
        return out


# Цены судьи за миллион токенов. Заданы руками: SDK их не отдаёт, а без них
# стоимость приёмки не попадает в суточный бюджет. Держи в актуальном состоянии.
PRICE_IN = float(os.getenv("JUDGE_PRICE_IN_USD_PER_MTOK", "3") or 3)
PRICE_OUT = float(os.getenv("JUDGE_PRICE_OUT_USD_PER_MTOK", "15") or 15)


def parse_judge_json(raw: str, require: tuple[str, ...] = ()) -> tuple[dict | None, bool]:
    """Вердикт судьи из ответа модели. Возвращает (данные, пришлось_извлекать).

    `require` — ключи, по которым опознаётся НУЖНЫЙ объект. Без них берётся
    первый попавшийся, и на живом прогоне плана это стоило тринадцати минут:
    модель начала прозой, в которой был свой маленький объект, его и вынули.
    Валидация честно сказала «tasks должен быть непустым списком», прогон ушёл
    на вторую попытку — а настоящий план лежал ниже в том же ответе.

    Судья просит СТРОГО JSON, но живая модель регулярно начинает с прозы:
    «diff is empty, I cannot verify…» — и только потом выдаёт объект. Поймано
    на прогоне по проекту 2. Строгий `json.loads` по всей строке на этом
    падал, и код честно ставил FAIL «вердикт не разобран».

    Направление безопасное, но это ЛОЖНЫЙ FAIL: он не находит дефект, а гонит
    задачу на повтор и дальше в эскалацию на ровном месте. Поэтому объект
    вырезается из текста.

    Часть FAIL при этом станет PASS — но только те, где модель вердикт
    вынесла, а мы его не прочитали. Флаг во втором элементе нужен, чтобы было
    видно, как часто это происходит: если часто — чинить надо промпт судьи,
    а не парсер.
    """
    text = (raw or "").replace("```json", "").replace("```", "").strip()
    if not text:
        return None, False
    try:
        data = json.loads(text)
        if isinstance(data, dict) and (not require or _has_keys(data, require)):
            return data, False
    except ValueError:
        pass

    first = None
    for data in _objects_in(text):
        if require and not _has_keys(data, require):
            first = first if first is not None else data
            continue
        return data, True
    # Ни один объект не содержит нужных ключей: отдаём первый, дальше его
    # отвергнет валидация — это честнее, чем молчаливое «ответа нет»
    return (first, True) if first is not None else (None, False)


def _has_keys(data: dict, keys) -> bool:
    return all(k in data for k in keys)


def _objects_in(text: str):
    """Сбалансированные JSON-объекты по порядку. Скобки внутри строк не считаем,
    иначе дефект с "}" в тексте оборвёт объект на середине.

    Оборванный ответ (упёрлись в max_tokens) сюда не попадает: закрывающей
    скобки просто нет, и выдумывать её мы не будем.
    """
    start = text.find("{")
    while start != -1:
        depth, in_str, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                    except ValueError:
                        break          # не тот объект — пробуем следующий «{»
                    if isinstance(data, dict):
                        yield data
                    break
        start = text.find("{", start + 1)


# Насколько дословной должна быть выдержка из критерия. Три буквы —
# не выдержка, а совпадение предлога.
MIN_PART_LEN = 6
# Доля слов выдержки, которые обязаны найтись в критерии. Не единица:
# модель склоняет слова и роняет предлоги, но выдумать целую фразу,
# наполовину совпадающую с критерием, ей уже негде.
PART_WORD_MATCH = 0.6
STEM_LEN = 5


def _stems(text: str) -> set[str]:
    """Слова с обрубленным окончанием: «подписи» и «подпись» — одно слово.

    Морфологии здесь нет и не надо: сравниваем выдержку с текстом, из
    которого её же и брали, а не два независимых предложения.
    """
    from .groups import normalize
    return {w[:STEM_LEN] for w in normalize(text).split() if len(w) > 2}


def part_of_criterion(part: str, criteria: str) -> bool:
    """Действительно ли выдержка взята из критерия.

    Ровно то же, что правило evidence в брифе, и с той же оговоркой: код
    проверяет ПРОИСХОЖДЕНИЕ ссылки, а не её уместность. Дефект, к которому
    приписана настоящая часть критерия не по делу, здесь пройдёт — судить
    об этом кодом нельзя. Зато дефект, не относящийся к критерию ни одним
    словом, дальше не идёт, а таких было девять из девяти на задаче 394.
    """
    from .groups import normalize
    p, c = normalize(part), normalize(criteria)
    if len(p) < MIN_PART_LEN or not c:
        return False
    if p in c:
        return True
    pw, cw = _stems(part), _stems(criteria)
    if not pw:
        return False
    return len(pw & cw) / len(pw) >= PART_WORD_MATCH


def _unpack(result) -> tuple[bool, str, list[str]]:
    """Проверка возвращает (ok, сообщение) или (ok, сообщение, замечания)."""
    if len(result) == 3:
        ok, msg, notes = result
        return bool(ok), str(msg), list(notes or [])
    ok, msg = result
    return bool(ok), str(msg), []


def defect_touches_criterion(defect: str, criteria: str) -> bool:
    """Говорит ли сам дефект о том же, о чём критерий.

    Второй рубеж, и он нужен: выдержка из критерия проверяет только
    происхождение ссылки, а сослаться можно на ОБЪЕКТ проверки, не на
    требование. «Нет middleware для сырого тела» с выдержкой «обработчика
    вебхука» — формально законная ссылка на настоящую часть критерия, а по
    сути опять общее ревью: про middleware в критерии не сказано ни слова.

    Поэтому дефект обязан задевать словарь критерия хотя бы одним словом.
    Порог низкий намеренно: настоящий дефект почти всегда называет то, чего
    не хватает («подпись не проверяется», «статус не меняется»), а замечание
    со стороны говорит о своём — про тесты, идемпотентность, импорты.
    """
    dw, cw = _stems(defect), _stems(criteria)
    return bool(dw & cw)


def attribute_defects(criteria: str, data: dict) -> tuple[list[str], list[str]]:
    """Дефекты, привязанные к критерию, и всё остальное — наблюдениями.

    Судья регулярно съезжает с приёмки на общее ревью кода: на живой задаче
    394 критерий «есть проверка подписи и переход заказа в paid» был выполнен
    буквально, а вердикт пришёл FAIL с девятью дефектами про middleware,
    идемпотентность, try/catch и отсутствие тестов. Замечания верные, но
    отвечают на вопрос, которого никто не задавал, — а задача от такого
    вердикта уходит на повтор и дальше в эскалацию.

    Поэтому дефект без выдержки из критерия в вердикт не попадает. Он не
    теряется: молча выбрасывать чужую работу — тот же грех, что молча
    пропускать проверку. Он переезжает в наблюдения, где его прочтёт
    владелец и, если сочтёт нужным, заведёт задачу.
    """
    kept, notes = [], []
    for raw in data.get("observations") or []:
        text = str(raw).strip()
        if text:
            notes.append(text)

    for item in data.get("defects") or []:
        if isinstance(item, dict):
            text = str(item.get("defect") or item.get("text") or "").strip()
            part = str(item.get("criterion_part") or "").strip()
        else:
            text, part = str(item).strip(), ""
        if not text:
            continue
        if not (part and part_of_criterion(part, criteria)):
            notes.append(f"вне критерия: {text}"
                         + (f" (судья сослался на «{part}»)" if part else ""))
        elif not defect_touches_criterion(text, criteria):
            notes.append(f"вне критерия: {text} (выдержка «{part}» из критерия, "
                         f"но сам дефект о другом)")
        else:
            kept.append(f"{text} [критерий: «{part}»]")
    return kept, notes


class Verifier:
    def __init__(self, backend=None):
        self._backend = backend
        self.client = None
        # без ключа судья недоступен. По умолчанию это ДЕФЕКТ, а не «ну и ладно»:
        # молча пропускать llm-приёмку — прямое противоречие смыслу модуля.
        # JUDGE_OPTIONAL=1 понижает до пропуска с предупреждением.
        self.judge_optional = str(os.getenv("JUDGE_OPTIONAL", "")).strip().lower() in ("1", "true", "yes")
        key = anthropic_key()
        if key:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=key)
        else:
            log.warning("ключа нет — llm-приёмка %s.\n%s",
                        "пропускается" if self.judge_optional else "будет валить задачи",
                        missing_secret_message("ANTHROPIC_API_KEY"))

    @property
    def backend(self):
        """Откуда берём вердикт: API или CLI. Задаётся LLM_BACKEND_JUDGE."""
        if self._backend is None:
            self._backend = llm.make("judge", client=self.client)
        return self._backend

    @property
    def uses_api(self) -> bool:
        """Нужен ли для приёмки ANTHROPIC_API_KEY.

        Ключ спрашиваем только у API-бэкенда: CLI платит квотой подписки и
        про ключ ничего не знает. Свойство было потеряно при переводе судьи
        на подписку — обе проверки, где оно упоминается, падали
        с AttributeError, и код записывал это в дефекты задачи, то есть
        любая llm-проверка проваливалась ещё до обращения к модели.
        """
        return getattr(self.backend, "name", llm.API) == llm.API

    # Тип проверки -> метод. Покрытие реестра проверяется тестом:
    # разрыв между тем, что планировщик выдаёт, и тем, что верификатор
    # умеет, однажды уже дал «приёмку без единой проверки».
    @property
    def handlers(self) -> dict:
        return {
            "shell": self._check_shell,
            "http": self._check_http,
            "file_exists": self._check_file_exists,
            "dom": self._check_dom,
            "screenshot": self._check_screenshot,
            "llm": self._check_llm,
            "human": self._check_human,
        }

    async def run(self, task: Task, project: Project) -> Verdict:
        cwd = project.workspace or str(cfg.workspaces / f"p{project.id}")
        defects: list[str] = []
        observations: list[str] = []
        executed = 0
        # считаем раздельно: кто вынес вердикт — код или модель
        deterministic = 0
        assisted = 0
        suspicious = 0

        for check in task.acceptance or []:
            if not isinstance(check, dict):
                defects.append(f"кривой критерий приёмки: {check!r}")
                continue
            kind = str(check.get("type") or "")
            handler = self.handlers.get(kind)
            if handler is None:
                # Раньше здесь стоял пропуск, и задача с одними неизвестными
                # критериями объявлялась принятой. Молчаливый пропуск проверки —
                # худший режим этой системы: он выглядит как успех
                defects.append(f"неизвестный тип проверки {kind!r} — приёмка невозможна")
                continue
            if not is_runnable(check):
                if kind in HUMAN_ONLY:
                    defects.append(f"human: {check.get('criteria') or 'нужен человек'}")
                else:
                    missing = [f for f in REGISTRY[kind].required
                               if not str(check.get(f) or "").strip()]
                    defects.append(f"{kind}: нет обязательных полей {missing} — "
                                   f"проверка не запустится")
                continue

            try:
                # проверка вправе вернуть третьим элементом замечания вне
                # критерия: на вердикт они не влияют, но показываются владельцу
                ok, msg, notes = _unpack(await handler(check, cwd, task, project))
                observations.extend(notes)
            except Exception as e:
                ok, msg = False, f"{kind}: {e}"
            executed += 1
            # проверка исполнена — записываем, чьим вердиктом она закрыта.
            # Считаем даже упавшую: счётчик отвечает на вопрос «чем эту задачу
            # вообще проверяют», а не «что прошло»
            s = spec(kind)
            if s is not None and s.deterministic:
                # Детерминированная — но доказывает ли она хоть что-нибудь?
                # `http 200` на корне сайта проходит и на пустом хостинге:
                # засчитывать её за доказательство значит закрывать задачу
                # по критерию, который не заметил бы её невыполнения
                if suspicious_reason(check):
                    suspicious += 1
                else:
                    deterministic += 1
            elif s is not None and s.assisted:
                assisted += 1
            if not ok:
                defects.append(msg)

        if executed == 0:
            # Ни одной исполнимой проверки. Это не «нечего проверять»,
            # это «проверить нечем» — и оно не может считаться приёмкой
            defects.append("нет ни одной исполнимой проверки — принять задачу нечем")

        notes = tuple(dict.fromkeys(observations))
        if notes:
            # Пишем здесь, а не у вызывающего: иначе один из двух путей
            # приёмки (планировщик и ручная отметка) однажды забудет это
            # сделать, и замечания судьи пропадут ровно там, где их не видно
            await self._save_observations(task, notes)
        return Verdict(ok=not defects, defects=defects,
                       deterministic=deterministic, assisted=assisted,
                       suspicious=suspicious, observations=notes)

    async def _save_observations(self, task: Task, notes: tuple[str, ...]) -> None:
        """Замечания вне критерия — владельцу, а не в никуда.

        На вердикт они не влияют по построению, но выбрасывать их нельзя:
        судья видит диф целиком и замечает настоящие вещи. Отсюда берутся
        следующие задачи, а не отказы принять эту.
        """
        async with Session() as s:
            row = await s.get(Task, task.id)
            if row is None:
                return
            row.observations = list(notes)
            await s.commit()
        log.info("задача %s: замечаний судьи вне критерия — %s (на приёмку "
                 "не влияют)", task.id, len(notes))

    # --- обёртки под единый контракт (check, cwd, task, project) ---

    async def _check_shell(self, check, cwd, task, project):
        return await self._shell(check["cmd"], cwd)

    async def _check_http(self, check, cwd, task, project):
        return await self._http(check["url"], int(check.get("expect", 200) or 200))

    async def _check_llm(self, check, cwd, task, project):
        return await self._judge(check.get("criteria", ""), cwd, task, project)

    async def _check_human(self, check, cwd, task, project):
        """Человеческая проверка машиной не выполняется — и не притворяется."""
        return False, f"human: {check.get('criteria') or 'нужна проверка глазами'}"

    # --- новые типы ---

    async def _check_file_exists(self, check, cwd, task, project):
        """Файл на месте. Путь строго внутри каталога проекта.

        Без песочницы критерий `../../etc/passwd` проходил бы всегда,
        а `C:/Windows/win.ini` превращал бы приёмку в фикцию.
        """
        root = Path(cwd).resolve()
        raw = str(check.get("path") or "")
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False, (f"file_exists: путь {raw!r} выходит за пределы каталога "
                           f"проекта — такой критерий не проверяется")
        if candidate.exists():
            return True, ""
        near = ", ".join(sorted(p.name for p in candidate.parent.iterdir())[:8]) \
            if candidate.parent.exists() else "каталога нет"
        return False, f"file_exists: нет {raw}. Рядом: {near}"

    async def _check_dom(self, check, cwd, task, project):
        """Селектор на живой странице. Через headless-браузер, а не разбор HTML:
        половина верстки сегодня дорисовывается скриптами, и по сырому
        HTML такой проверки не сделать."""
        url = str(check.get("url") or "")
        selector = str(check.get("selector") or "")
        need = int(check.get("min_count", 1) or 1)
        timeout = float(check.get("timeout", DEFAULT_TIMEOUT_SEC) or DEFAULT_TIMEOUT_SEC)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return False, ("dom: playwright не установлен — проверка невыполнима "
                           "(pip install playwright && playwright install chromium)")

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    page = await browser.new_page()
                    page.set_default_timeout(timeout * 1000)
                    await page.goto(url, wait_until="networkidle",
                                    timeout=timeout * 1000)
                    count = await page.locator(selector).count()
                    if count >= need:
                        return True, ""
                    title = (await page.title())[:80]
                    body = (await page.inner_text("body"))[:200].replace("\n", " ")
                    return False, (f"dom: {url} — селектор {selector!r} найден {count} раз, "
                                   f"нужно {need}. Заголовок: {title!r}. "
                                   f"Начало страницы: {body!r}")
                finally:
                    await browser.close()
        except Exception as e:
            return False, f"dom: {url} не проверить — {type(e).__name__}: {e}"

    async def _check_screenshot(self, check, cwd, task, project):
        """Снимки на трёх ширинах и вердикт модели по описанию критерия.

        Это assisted-проверка: модель видит картинку и говорит, похоже ли на
        описанное. Приговором такой вердикт не считается — но он ловит
        разъехавшуюся вёрстку, которую селектором не поймать.
        """
        url = str(check.get("url") or "")
        criteria = str(check.get("criteria") or "")
        timeout = float(check.get("timeout", DEFAULT_TIMEOUT_SEC) or DEFAULT_TIMEOUT_SEC)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return False, "screenshot: playwright не установлен — проверка невыполнима"
        if self.uses_api and self.client is None:
            return False, ("screenshot: судить снимок нечем — "
                           + missing_secret_message("ANTHROPIC_API_KEY").splitlines()[0])

        shots: list[tuple[str, bytes]] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    for width, height in VIEWPORTS:
                        page = await browser.new_page(viewport={"width": width,
                                                                "height": height})
                        page.set_default_timeout(timeout * 1000)
                        await page.goto(url, wait_until="networkidle",
                                        timeout=timeout * 1000)
                        shots.append((f"{width}px", await page.screenshot(full_page=False)))
                        await page.close()
                finally:
                    await browser.close()
        except Exception as e:
            return False, f"screenshot: {url} не снять — {type(e).__name__}: {e}"

        content = [{"type": "text",
                    "text": (f"Критерий: {criteria}\n\nНиже снимки одной страницы на трёх "
                             f"ширинах. Ответь СТРОГО JSON: "
                             f'{{"verdict":"PASS|FAIL","reason":"коротко"}}')}]
        for label, png in shots:
            content.append({"type": "text", "text": f"Ширина {label}:"})
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": "image/png",
                                       "data": base64.b64encode(png).decode()}})

        try:
            reply = await self._ask(task, project, content=content, max_tokens=800)
        except LLMError as e:
            return False, f"screenshot: {e}"
        raw = reply.text
        data, extracted = parse_judge_json(raw, require=("verdict",))
        if extracted:
            log.warning("судья по снимку начал ответ прозой, вердикт извлечён "
                        "из текста (задача %s)", getattr(task, "id", "?"))
        if data is None:
            return False, f"screenshot: судья вернул не-JSON: {raw[:200]!r}"
        if str(data.get("verdict")).upper() == "PASS":
            return True, ""
        return False, f"screenshot: {data.get('reason') or 'не соответствует описанию'}"

    # --- уровень 1: детерминированные ---

    async def _shell(self, cmd: str, cwd: str) -> tuple[bool, str]:
        os.makedirs(cwd, exist_ok=True)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()                      # иначе процесс переживёт автопилот
            await proc.wait()
            return False, f"`{cmd}` не уложился в {SHELL_TIMEOUT_SEC}s"
        if proc.returncode == 0:
            return True, ""
        tail = (out or b"").decode(errors="replace")[-1200:]
        return False, f"`{cmd}` exit={proc.returncode}\n{tail}"

    async def _http(self, url: str, expect: int) -> tuple[bool, str]:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code == expect:
            return True, ""
        return False, f"GET {url} -> {r.status_code}, ожидалось {expect}"

    # --- уровень 3: судья ---

    async def _judge(self, criteria: str, cwd: str, task: Task,
                     project: Project) -> tuple[bool, str, list[str]]:
        if self.uses_api and not self.client:
            if self.judge_optional:
                log.warning("судья пропущен (нет ANTHROPIC_API_KEY), задача %s", task.id)
                return True, "", []
            return False, "судья недоступен: " + missing_secret_message("ANTHROPIC_API_KEY"), []

        diff = await self._diff(cwd)
        msg = JUDGE_PROMPT.format(
            criteria=criteria or task.title,
            diff=diff[:60000],
            report=task.last_error or "—",
        )
        reply = await self._ask(task, project, prompt=msg, max_tokens=JUDGE_MAX_TOKENS)
        raw = reply.text
        data, extracted = parse_judge_json(raw, require=("verdict",))
        if extracted:
            # считаем такие случаи: если их много, чинить надо промпт судьи,
            # а не парсер — модель систематически не слушает «СТРОГО JSON»
            log.warning("судья начал ответ прозой, вердикт извлечён из текста "
                        "(задача %s): %r", task.id, raw[:200])
        if data is None:
            # молча засчитывать PASS нельзя: непрочитанный вердикт — это не приёмка
            log.warning("судья вернул не-JSON для задачи %s: %r", task.id, raw[:300])
            return False, "судья не ответил: вердикт не разобран", []

        text = criteria or task.title
        defects, notes = attribute_defects(text, data)
        if notes:
            log.info("судья: замечаний вне критерия %s (задача %s) — на вердикт "
                     "не влияют", len(notes), task.id)
        if str(data.get("verdict", "")).upper() == "PASS":
            return True, "", notes
        if not defects:
            # FAIL, ни один дефект не про критерий. Держать такой вердикт —
            # значит гнать выполненную задачу на повтор и в эскалацию
            # за грехи, о которых её никто не спрашивал. Ровно это и
            # случилось на задаче 394
            log.warning("судья поставил FAIL задаче %s, но ни один дефект не "
                        "привязан к критерию — принимаю. Замечания сохранены "
                        "(%s шт.)", task.id, len(notes))
            return True, "", notes
        return False, "судья: " + "; ".join(defects), notes

    async def _diff(self, cwd: str) -> str:
        """git diff без shell-специфики: `2>/dev/null ||` под Windows не работает."""
        for args in (["git", "--no-pager", "diff", "HEAD~1"], ["git", "--no-pager", "diff"]):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await proc.communicate()
            except (FileNotFoundError, NotADirectoryError):
                return ""
            if proc.returncode == 0 and out.strip():
                return out.decode(errors="replace")
        return ""

    async def _ask(self, task: Task, project: Project, *, prompt: str = "",
                   content=None, max_tokens: int = 800):
        """Вызов судьи со строкой расхода, заведённой ЗАРАНЕЕ.

        Строка появляется до вызова, а не после: иначе прогон, убитый на
        середине, не оставляет следа — квота сожжена, а в учёте пусто.
        Незакрытая строка (`finished_at` пуст) как раз и означает «вызов
        не вернулся».
        """
        run_id = await open_run(task.id, "judge")
        t0 = time.monotonic()
        try:
            reply = await self.backend.ask(prompt, content=content,
                                           model=cfg.judge_model,
                                           max_tokens=max_tokens)
        except BaseException:
            await close_run(run_id, ok=False,
                            backend=getattr(self.backend, "name", llm.API),
                            cost_usd=0.0, seconds=time.monotonic() - t0)
            raise
        await self._charge(task, project, reply, run_id)
        return reply

    async def _charge(self, task: Task, project: Project, reply, run_id: int) -> None:
        """Приёмка тоже стоит ресурса. У CLI это квота подписки, а не деньги:
        складывать одно с другим — соврать в обе стороны сразу."""
        await close_run(run_id, ok=True, backend=reply.backend,
                        cost_usd=reply.cost_usd, seconds=reply.seconds)
        async with Session() as s:
            t = await s.get(Task, task.id)
            if t is not None and reply.billed:
                t.cost_usd += reply.cost_usd
            pr = await s.get(Project, project.id)
            if pr is not None and reply.billed:
                pr.cost_usd += reply.cost_usd
            await s.commit()
