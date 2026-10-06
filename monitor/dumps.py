"""Дампы при сбоях: HTML страницы, снимок экрана (JPEG), начало файла фида и описание — для разбора причины.

Папка: data/failures/<дата-время>-<сеть>-<причина>/{meta.json, page.html.gz, screen.jpg, feed-head.gz}.
Хранится не больше MAX_ITEMS дампов и MAX_BYTES в сумме: старые удаляются сами.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import time
from typing import Any

from . import config, log

MAX_ITEMS = 60
MAX_BYTES = 200_000_000
FEED_HEAD_BYTES = 256_000

REASONS = {
    "captcha": "проверка браузера / капча",
    "429": "сайт ответил 429 (слишком много запросов)",
    "denied": "доступ закрыт (401/403/503)",
    "empty": "на странице нет товаров",
    "no-price": "на карточке нет цены",
    "open-failed": "страница не открылась",
    "feed-broken": "фид повреждён или не читается",
    "feed-empty": "в фиде нет нужных товаров",
}


def folder() -> str:
    return os.path.join(config.DATA, "failures")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:30] or "fail"


def save(
    site: str,
    reason: str,
    *,
    url: str = "",
    status: int | None = None,
    title: str = "",
    html: str | None = None,
    screenshot_b64: str | None = None,
    feed_path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str | None:
    """Сохраняет дамп. Никогда не роняет сбор: при ошибке записи возвращает None."""
    try:
        base = folder()
        os.makedirs(base, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{_slug(site)}-{_slug(reason)}"
        d = os.path.join(base, name)
        n = 1
        while os.path.exists(d):
            n += 1
            d = os.path.join(base, f"{name}-{n}")
        os.makedirs(d)
        meta = {
            "site": site,
            "reason": reason,
            "reason_text": REASONS.get(reason, reason),
            "url": url,
            "status": status,
            "title": title,
            "ts": time.time(),
            **log.current_context(),
            **(extra or {}),
        }
        files = []
        if html:
            with gzip.open(os.path.join(d, "page.html.gz"), "wt", encoding="utf-8") as f:
                f.write(html)
            files.append("page.html.gz")
        if screenshot_b64:
            with open(os.path.join(d, "screen.jpg"), "wb") as f:
                f.write(base64.b64decode(screenshot_b64))
            files.append("screen.jpg")
        if feed_path and os.path.exists(feed_path):
            from .feeds import _open  # распакованное начало файла

            with _open(feed_path) as src, gzip.open(os.path.join(d, "feed-head.gz"), "wb") as out:
                out.write(src.read(FEED_HEAD_BYTES))
            files.append("feed-head.gz")
        meta["files"] = files
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        log.warning(
            "dump.saved",
            f"Сохранён дамп сбоя: {REASONS.get(reason, reason)}",
            site=site,
            reason=reason,
            url=url or None,
            path=d,
            files=files,
        )
        prune()
        return d
    except (OSError, ValueError) as e:
        log.error("dump.failed", f"Дамп сбоя не сохранился: {e}", site=site, reason=reason)
        return None


def _size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def prune(max_items: int = MAX_ITEMS, max_bytes: int = MAX_BYTES) -> int:
    """Удаляет самые старые дампы сверх лимитов. Возвращает, сколько удалено."""
    base = folder()
    if not os.path.isdir(base):
        return 0
    items = sorted((os.path.join(base, n) for n in os.listdir(base)), key=os.path.getmtime, reverse=True)
    keep, total, removed = 0, 0, 0
    for p in items:
        size = _size(p)
        if keep < max_items and total + size <= max_bytes:
            keep += 1
            total += size
            continue
        shutil.rmtree(p, ignore_errors=True)
        removed += 1
    return removed


def recent(n: int = 20) -> list[dict[str, Any]]:
    base = folder()
    if not os.path.isdir(base):
        return []
    out = []
    for name in sorted(os.listdir(base), reverse=True)[:n]:
        try:
            with open(os.path.join(base, name, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        meta["dir"] = os.path.join(base, name)
        out.append(meta)
    return out
