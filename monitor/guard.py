"""Защита источников: повторы с нарастающей паузой, предохранитель (circuit breaker), пауза после 429 и капчи.

Источник — пара «сеть × способ» (lemanapro/feed, petrovich/edge…). Состояния предохранителя:
  closed — работаем как обычно;
  open   — источник на паузе до `until`, сбор его пропускает;
  half   — пауза прошла, одна пробная попытка: удалась — closed, не удалась — снова open с удвоенной паузой.

Правила:
  • 429 или капча/отказ сайта → пауза сразу: не меньше часа (и не меньше Retry-After), при повторах 2 ч, 4 ч… до 24 ч;
  • обычные сбои (сеть, 5xx, страница не открылась) → после 3 сбоев подряд пауза 1 ч, дальше так же удваивается;
  • удачный сбор сбрасывает счётчики.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from typing import Any, TypeVar

from . import log

BLOCK_PAUSE = 3600.0
ERROR_PAUSE = 3600.0
MAX_PAUSE = 24 * 3600.0
FAIL_THRESHOLD = 3

T = TypeVar("T")


class Blocked(Exception):
    """Источник просит остановиться: 429, капча, отказ в доступе. Не повторяем — ставим на паузу."""

    def __init__(self, reason: str, retry_after: float | None = None, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.retry_after = retry_after
        self.detail = detail


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After: секунды или дата HTTP."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        import email.utils

        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- повторы


def backoff_delays(attempts: int, base: float, factor: float = 4.0, max_delay: float = 120.0) -> list[float]:
    """Паузы между попытками: base, base·factor, … (с разбросом ±20 %), не больше max_delay."""
    return [min(max_delay, base * factor**i) * random.uniform(0.8, 1.2) for i in range(max(0, attempts - 1))]


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base: float = 2.0,
    factor: float = 4.0,
    max_delay: float = 120.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], Any] = time.sleep,
    on_retry: Callable[[int, BaseException, float], Any] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> T:
    """Вызывает fn; при ошибке из retry_on ждёт и пробует снова. Blocked не повторяется никогда."""
    delays = backoff_delays(attempts, base, factor, max_delay)
    for i in range(attempts):
        try:
            return fn()
        except Blocked:
            raise
        except retry_on as e:
            if i >= attempts - 1 or (cancelled and cancelled()):
                raise
            delay = delays[i]
            if on_retry:
                on_retry(i + 1, e, delay)
            sleep(delay)
    raise RuntimeError("недостижимо")  # pragma: no cover


# ---------------------------------------------------------------- предохранитель


def _row(con: sqlite3.Connection, site: str, source: str) -> dict[str, Any]:
    r = con.execute("SELECT * FROM breakers WHERE site=? AND source=?", (site, source)).fetchone()
    return (
        dict(r)
        if r
        else {
            "site": site,
            "source": source,
            "state": "closed",
            "failures": 0,
            "opens": 0,
            "opened_at": None,
            "until": None,
            "reason": None,
        }
    )


def _save(con: sqlite3.Connection, b: dict[str, Any]) -> None:
    con.execute(
        "INSERT OR REPLACE INTO breakers(site,source,state,failures,opens,opened_at,until,reason) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (b["site"], b["source"], b["state"], b["failures"], b["opens"], b["opened_at"], b["until"], b["reason"]),
    )


def allow(con: sqlite3.Connection, site: str, source: str, now: float | None = None) -> tuple[bool, dict[str, Any]]:
    """Можно ли идти к источнику. Пауза кончилась — пробная попытка (half)."""
    now = now or time.time()
    b = _row(con, site, source)
    if b["state"] == "open":
        if b["until"] and now < b["until"]:
            return False, b
        b["state"] = "half"
        _save(con, b)
        log.info("breaker.half", f"{site}/{source}: пауза закончилась, пробная попытка", site=site, source=source)
    return True, b


def success(con: sqlite3.Connection, site: str, source: str) -> None:
    b = _row(con, site, source)
    if b["state"] != "closed" or b["failures"]:
        log.info("breaker.closed", f"{site}/{source}: источник снова в порядке", site=site, source=source)
    b.update(state="closed", failures=0, opens=0, opened_at=None, until=None, reason=None)
    _save(con, b)


def _open(con: sqlite3.Connection, b: dict[str, Any], pause: float, reason: str, now: float) -> float:
    until = now + pause
    b.update(state="open", opened_at=now, until=until, reason=reason, opens=b["opens"] + 1)
    _save(con, b)
    log.warning(
        "breaker.open",
        f"{b['site']}/{b['source']}: пауза {pause / 3600:.1f} ч — {reason}",
        site=b["site"],
        source=b["source"],
        until=until,
        pause_s=round(pause),
        reason=reason,
    )
    return until


def blocked(
    con: sqlite3.Connection,
    site: str,
    source: str,
    reason: str,
    retry_after: float | None = None,
    now: float | None = None,
) -> float:
    """429 / капча / отказ: пауза сразу. Возвращает время окончания паузы."""
    now = now or time.time()
    b = _row(con, site, source)
    pause = min(MAX_PAUSE, max(BLOCK_PAUSE * 2 ** b["opens"], retry_after or 0))
    b["failures"] += 1
    return _open(con, b, pause, reason, now)


def failure(con: sqlite3.Connection, site: str, source: str, reason: str, now: float | None = None) -> float | None:
    """Обычный сбой. Пауза — после FAIL_THRESHOLD подряд или если не удалась пробная попытка."""
    now = now or time.time()
    b = _row(con, site, source)
    b["failures"] += 1
    if b["state"] == "half" or b["failures"] >= FAIL_THRESHOLD:
        return _open(con, b, min(MAX_PAUSE, ERROR_PAUSE * 2 ** b["opens"]), reason, now)
    b["reason"] = reason
    _save(con, b)
    log.info(
        "breaker.failure",
        f"{site}/{source}: сбой {b['failures']} из {FAIL_THRESHOLD} — {reason}",
        site=site,
        source=source,
        failures=b["failures"],
    )
    return None


def reset(con: sqlite3.Connection, site: str, source: str) -> None:
    """Снять паузу вручную (кнопка «Снять паузу»)."""
    b = _row(con, site, source)
    b.update(state="closed", failures=0, opens=0, opened_at=None, until=None, reason=None)
    _save(con, b)
    log.info("breaker.reset", f"{site}/{source}: пауза снята вручную", site=site, source=source)


def states(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT * FROM breakers ORDER BY site, source")]
