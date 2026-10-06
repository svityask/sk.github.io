"""Структурированный журнал: одна строка JSON на событие, с уровнем и контекстом.

    from monitor import log
    with log.context(run_id=12, site="lemanapro"):
        log.info("feed.fetched", "Фид скачан", bytes=123, status="скачан")

Файл: data/logs/monitor.jsonl. Ротация по размеру (5 МБ × 5 файлов). Строка:
    {"ts": "2026-10-06T08:31:02+03:00", "level": "INFO", "event": "feed.fetched", "msg": "Фид скачан",
     "run_id": 12, "site": "lemanapro", "bytes": 123, "status": "скачан"}
Запись никогда не роняет сбор: ошибка записи журнала молча пропускается.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import sys
import threading
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from . import config

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
FILE_NAME = "monitor.jsonl"
MAX_BYTES = 5_000_000
KEEP_FILES = 5

_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("log_ctx", default=None)
_lock = threading.Lock()
_min_level = LEVELS.get(os.environ.get("OSNOVIT_LOG_LEVEL", "INFO").upper(), 20)
_echo = os.environ.get("OSNOVIT_LOG_ECHO") == "1"


def path() -> str:
    return os.path.join(config.LOGS_DIR, FILE_NAME)


@contextlib.contextmanager
def context(**fields: Any) -> Iterator[None]:
    """Поля, которые попадут во все записи внутри блока (run_id, site, source…)."""
    token = _ctx.set({**(_ctx.get() or {}), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _ctx.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_ctx.get() or {})


def _rotate(p: str) -> None:
    if not os.path.exists(p) or os.path.getsize(p) < MAX_BYTES:
        return
    for i in range(KEEP_FILES - 1, 0, -1):
        src = f"{p}.{i}"
        if os.path.exists(src):
            os.replace(src, f"{p}.{i + 1}")
    os.replace(p, f"{p}.1")
    extra = f"{p}.{KEEP_FILES}"
    if os.path.exists(extra):
        os.remove(extra)


def write(level: str, event: str, msg: str = "", **fields: Any) -> dict[str, Any] | None:
    level = level.upper()
    if LEVELS.get(level, 20) < _min_level:
        return None
    ts = datetime.now().astimezone().isoformat(timespec="seconds")  # strftime('%z') на Windows отдаёт имя пояса
    rec: dict[str, Any] = {"ts": ts, "level": level, "event": event, "msg": msg}
    rec.update(_ctx.get() or {})
    rec.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(rec, ensure_ascii=False, default=str)
    try:
        config.ensure_dirs()
        with _lock:
            p = path()
            _rotate(p)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    if _echo:
        print(line, file=sys.stderr)
    return rec


def debug(event: str, msg: str = "", **fields: Any) -> None:
    write("DEBUG", event, msg, **fields)


def info(event: str, msg: str = "", **fields: Any) -> None:
    write("INFO", event, msg, **fields)


def warning(event: str, msg: str = "", **fields: Any) -> None:
    write("WARNING", event, msg, **fields)


def error(event: str, msg: str = "", **fields: Any) -> None:
    write("ERROR", event, msg, **fields)


def tail(n: int = 200, min_level: str = "DEBUG", run_id: int | None = None) -> list[dict[str, Any]]:
    """Последние n записей (из текущего и предыдущего файла), новые — в конце."""
    want = LEVELS.get(min_level.upper(), 10)
    out: list[dict[str, Any]] = []
    for p in (f"{path()}.1", path()):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if LEVELS.get(rec.get("level", "INFO"), 20) < want:
                    continue
                if run_id is not None and rec.get("run_id") != run_id:
                    continue
                out.append(rec)
    return out[-n:]
