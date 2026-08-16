"""Прерывание прогона: сигналы, дочерние процессы, блокировка проекта.

Три вещи, каждая из которых уже стоила испорченных данных.

**Оборванный прогон обязан прибраться за собой.** `brief_eval` чистит бриф
перед полным пересбором и возвращает прежний, если прогон не удался. Это
работало на исключениях — и не работало на сигналах: SIGTERM убивает
процесс, до `except` дело не доходит, бриф остаётся обнулённым. Здесь
сигнал превращается в отмену задачи, то есть в обычное исключение, которое
код восстановления увидит.

**Дочерний claude обязан умереть вместе с родителем.** Убитый прогон уже
переживал родителя: процесс продолжал жечь квоту пятичасового окна и
дописывал строки в `runs` от имени сессии, которую никто не ждёт. Здесь два
рубежа: явное убийство при выходе и — на Windows — job object с
`KILL_ON_JOB_CLOSE`, который срабатывает даже при `taskkill /F`, когда
никакой обработчик выполниться уже не может.

**Один проект — один пишущий процесс.** Два `plan_eval` на одном проекте
писали задачи через `_persist` одновременно и перемешали план. Блокировка
файловая, поэтому переживает падение процесса: лочит ОС, а не наша запись
в файле.
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import signal
import sys
import time

from .config import cfg

log = logging.getLogger("guard")

WINDOWS = sys.platform == "win32"


class Interrupted(BaseException):
    """Прогон прерван сигналом.

    Наследуется от BaseException намеренно, как KeyboardInterrupt: широкий
    `except Exception` в чужом коде не должен проглотить остановку и сделать
    вид, что всё идёт по плану.
    """


class BusyProject(RuntimeError):
    """Проект уже занят другим процессом. Не ошибка, а отказ стартовать."""


# ---------- дочерние процессы ----------

_children: set = set()
_job = None                     # handle job object на Windows, см. _win_job()


def _win_job():
    """Job object, убивающий детей при закрытии — то есть при смерти родителя.

    Единственный способ на Windows гарантировать, что дочерний claude не
    переживёт нас при жёстком убийстве: `TerminateProcess` не даёт выполнить
    ни обработчик сигнала, ни atexit. Handle держим в глобальной переменной
    — он закроется вместе с процессом, и job погасит всё, что в нём осталось.

    Не получилось — работаем без него: остаётся мягкий путь (kill_children).
    """
    global _job
    if _job is not None or not WINDOWS:
        return _job
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            k32.CloseHandle(handle)
            return None
        _job = handle
    except Exception as e:                                # noqa: BLE001
        log.debug("job object недоступен (%s) — остаётся мягкое убийство", e)
        _job = None
    return _job


def _join_job(pid: int) -> None:
    if not WINDOWS:
        return
    job = _win_job()
    if not job:
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        proc = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not proc:
            return
        try:
            k32.AssignProcessToJobObject(job, proc)
        finally:
            k32.CloseHandle(proc)
    except Exception as e:                                # noqa: BLE001
        log.debug("не смог посадить %s в job: %s", pid, e)


def watch_child(proc) -> None:
    """Взять дочерний процесс под присмотр: он не должен нас пережить."""
    _children.add(proc)
    pid = getattr(proc, "pid", None)
    if pid:
        _join_job(pid)


def forget_child(proc) -> None:
    _children.discard(proc)


def kill_children() -> int:
    """Убить всё, что ещё живо. Возвращает, скольких пришлось убить."""
    killed = 0
    for proc in list(_children):
        try:
            if proc.returncode is None:
                proc.kill()
                killed += 1
        except (ProcessLookupError, OSError):
            pass
        finally:
            _children.discard(proc)
    if killed:
        log.warning("убито дочерних процессов: %s — иначе они дожигали бы "
                    "квоту в одиночку", killed)
    return killed


atexit.register(kill_children)


# ---------- сигналы ----------

_SIGNALS = [signal.SIGINT, signal.SIGTERM]
if hasattr(signal, "SIGBREAK"):                            # Windows Ctrl+Break
    _SIGNALS.append(signal.SIGBREAK)

_interrupted = False


def interrupted() -> bool:
    return _interrupted


@contextlib.contextmanager
def _catch_signals(task: asyncio.Task):
    """Сигнал → отмена задачи, то есть обычное исключение внутри корутины.

    Так восстановление в `except` действительно отрабатывает. Второй сигнал
    подряд — жёсткий выход: если уборка сама зависла, ждать её нечего.
    """
    global _interrupted
    previous = {}

    def handler(signum, _frame):
        global _interrupted
        if _interrupted:                       # второй раз — уже без реверансов
            _restore()
            kill_children()
            raise KeyboardInterrupt
        _interrupted = True
        log.warning("получен сигнал %s — прерываю прогон и убираю за собой",
                    getattr(signum, "name", signum))
        kill_children()
        task.cancel()

    def _restore():
        for sig, old in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, old)
        previous.clear()

    for sig in _SIGNALS:
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            # не главный поток или платформа не умеет этот сигнал
            continue
    try:
        yield
    finally:
        _restore()


async def _ticker() -> None:
    """Пустой цикл, чтобы обработчик сигнала выполнился вовремя.

    Обработчики Python выполняются между байткодами. Пока цикл событий спит
    на ожидании ввода-вывода, байткоды не выполняются, и сигнал может ждать
    очень долго — на Windows это особенно заметно. Тик раз в пятую секунды
    стоит ничего и делает остановку предсказуемой.
    """
    while True:
        await asyncio.sleep(0.2)


def run(coro):
    """`asyncio.run` с уборкой: сигнал прерывает прогон, дети не переживают.

    Прерванный прогон поднимает `Interrupted` — вызывающий скрипт печатает
    внятное сообщение вместо простыни трассировки.
    """
    global _interrupted
    _interrupted = False

    async def _main():
        task = asyncio.current_task()
        ticker = asyncio.create_task(_ticker())
        with _catch_signals(task):
            try:
                return await coro
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    try:
        return asyncio.run(_main())
    except asyncio.CancelledError:
        if _interrupted:
            raise Interrupted("прогон прерван сигналом") from None
        raise
    except KeyboardInterrupt:
        raise Interrupted("прогон прерван с клавиатуры") from None
    finally:
        kill_children()


# ---------- блокировка проекта ----------

LOCK_DIR = cfg.root / "locks"
# Лочим байт далеко за концом файла: сам текст с pid и назначением остаётся
# читаемым, иначе второй процесс не смог бы сказать, кто держит замок.
LOCK_OFFSET = 1 << 30

_held: dict[int, str] = {}          # что этот процесс уже держит


def _lock_file(project_id: int):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR / f"p{project_id}.lock"


def _try_lock(fd: int) -> bool:
    try:
        if WINDOWS:
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, LOCK_OFFSET,
                        os.SEEK_SET)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if WINDOWS:
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, LOCK_OFFSET, os.SEEK_SET)


@contextlib.contextmanager
def project_lock(project_id: int, purpose: str):
    """Один пишущий процесс на проект. Второй отказывается стартовать.

    Отказ — это правильный исход, а не грубость: два прогона на одном проекте
    пишут задачи и бриф вперемешку, и разбирать получившееся приходится
    руками по одной строке. Уже случалось: два `plan_eval` на проекте 8.

    Замок держит ОС, поэтому упавший процесс освобождает его сам — файлов
    с чужим pid, которые надо удалять руками, здесь не бывает.
    """
    if project_id in _held:
        raise BusyProject(
            f"проект {project_id} уже занят в этом же процессе: "
            f"{_held[project_id]}. Два прогона разом перемешают результат.")

    path = _lock_file(project_id)
    # O_BINARY обязателен на Windows: в текстовом режиме CRT правит переводы
    # строк, а мы работаем со смещениями в байтах
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    if not _try_lock(fd):
        holder = ""
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 400).decode("utf-8", "replace").strip()
        os.close(fd)
        raise BusyProject(
            f"проект {project_id} уже занят другим процессом"
            + (f" ({holder})" if holder else "")
            + f".\nЭтот прогон ({purpose}) не начат намеренно: два процесса "
              f"на одном проекте пишут результат вперемешку.\n"
              f"Дождись первого или сними его, а не запускай второй.")

    stamp = f"{purpose}, pid {os.getpid()}, с {time.strftime('%Y-%m-%d %H:%M:%S')}"
    with contextlib.suppress(OSError):
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, stamp.encode("utf-8"))
        os.ftruncate(fd, len(stamp.encode("utf-8")))
    _held[project_id] = stamp
    log.debug("проект %s занят: %s", project_id, stamp)
    try:
        yield
    finally:
        _held.pop(project_id, None)
        _unlock(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
