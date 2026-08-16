"""Упор в квоту подписки — общее состояние процесса.

Упор принципиально отличается от ошибки, и раньше этой разницы не было,
потому что API упирался только в деньги. Теперь любой вызов может вернуть
«приходи через два часа», и обойтись с этим как с провалом нельзя:

* **попытка не засчитывается.** Четыре упора подряд увели бы живую задачу
  в `escalated`, хотя с работой всё в порядке;
* **бриф не обнуляется и не считается несобранным.** Накопленное остаётся;
* **встаёт вся работа, а не одна задача.** Квота общая на процесс: пока она
  исчерпана, следующий вызов упрётся ровно так же;
* **владельцу сообщаем один раз за период.** Уведомление на каждый вызов
  превращает телефон в будильник и перестаёт читаться.

Состояние держится в памяти процесса намеренно: квота — свойство текущего
пятичасового окна, а не факт, который надо помнить между перезапусками.
"""
from __future__ import annotations

import logging
import time

from .config import cfg

log = logging.getLogger("limit")


class LimitState:
    """Когда квота освободится и сообщили ли мы об этом владельцу."""

    def __init__(self):
        self.blocked_until: float = 0.0
        self.reason: str = ""
        self.hits: int = 0
        # None, а не 0: с нулём ПЕРВОЕ уведомление подавлялось —
        # `now - 0` меньше периода, и владелец не узнавал об упоре вовсе
        self._notified_at: float | None = None
        self._backoff: float = float(cfg.limit_backoff_start_sec)

    # ---------- запросы ----------

    def blocked(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) < self.blocked_until

    def seconds_left(self, now: float | None = None) -> float:
        return max(0.0, self.blocked_until - (now or time.monotonic()))

    def should_notify(self, now: float | None = None) -> bool:
        """Уведомлять не чаще раза за период — иначе это спам, а не сигнал."""
        now = time.monotonic() if now is None else now
        if (self._notified_at is not None
                and now - self._notified_at < cfg.limit_notify_every_sec):
            return False
        self._notified_at = now
        return True

    # ---------- события ----------

    def hit(self, reason: str = "", retry_after: float | None = None,
            now: float | None = None) -> float:
        """Зафиксировать упор. Возвращает, на сколько секунд встаём.

        Если CLI сказал время сброса — верим ему. Не сказал — экспоненциальная
        пауза: ломиться в закрытую дверь раз в минуту значит не заметить,
        что она закрыта, и сжечь остаток окна на пустые попытки.
        """
        now = now or time.monotonic()
        self.hits += 1
        self.reason = str(reason or "")[:300]
        if retry_after and retry_after > 0:
            wait = float(retry_after)
            self._backoff = float(cfg.limit_backoff_start_sec)
        else:
            wait = self._backoff
            self._backoff = min(self._backoff * 2, float(cfg.limit_backoff_max_sec))
        self.blocked_until = max(self.blocked_until, now + wait)
        log.warning("упор в квоту (%s-й): встаём на %.0f минут. %s",
                    self.hits, wait / 60, self.reason)
        return wait

    def clear(self) -> None:
        """Вызов прошёл — окно снова наше.

        Сбрасывается и таймер уведомления: инцидент закончился, и о следующем
        упоре владелец должен узнать сразу, а не через час молчания. Если
        квота начнёт мигать, частые сообщения — это и есть правильный сигнал.
        """
        if self.hits or self.blocked_until:
            log.info("квота снова доступна после %s упоров", self.hits)
        self.blocked_until = 0.0
        self.hits = 0
        self.reason = ""
        self._notified_at = None
        self._backoff = float(cfg.limit_backoff_start_sec)

    def message(self) -> str:
        mins = self.seconds_left() / 60
        return (f"Упёрлись в квоту подписки. Работа приостановлена примерно "
                f"на {mins:.0f} минут.\n{self.reason}\n"
                f"Задачи не провалены и попытки не засчитаны — продолжим, "
                f"когда окно откроется.")


# Одно на процесс: квота общая, и держать её по объектам бессмысленно
state = LimitState()
