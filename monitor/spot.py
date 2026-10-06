"""Выборочная проверка: человек сверяет несколько карточек с сайтом — так видно, можно ли доверять отчёту.

Выборка — 30 товаров из последних сборов: сначала Основит, остальное поровну по сетям, случайно. Каждый товар
отмечается «Верно» или ошибкой (цена, фасовка, вид, не конкурент). Ошибка фасовки и вида сразу исправляется
ручной правкой — так проверка одновременно учит приложение. Точность считается за последние 90 дней.
"""

from __future__ import annotations

import random
import time
from typing import Any

from . import analysis, db, kinds, sites, units

SAMPLE_KEY = "spot_sample"
VERDICTS = {
    "ok": "верно",
    "price": "цена не та",
    "pack": "фасовка не та",
    "kind": "вид не тот",
    "not_competitor": "не конкурент",
}
WINDOW = 90 * 86400  # точность — за последние 90 дней
RECHECK_AFTER = 30 * 86400  # проверенный товар снова попадает в выборку не раньше чем через 30 дней


def _row(con, key):
    r = con.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def enrich(p: dict[str, Any], kind_ov, pack_ov, decisions, brand) -> dict[str, Any]:
    p["is_ours"], _ = analysis.is_ours(p.get("name"), p.get("vendor"), brand)
    p["per_unit"] = units.per_unit(p.get("price"), p.get("pack_qty"))
    p["pack"] = units.pack_label(p.get("pack_qty"), p.get("pack_unit"))
    a, manual = kinds.attrs_of(p, kind_ov)
    p["kind"], p["kind_manual"] = kinds.label(a) or "", manual
    p["pack_manual"] = p["key"] in pack_ov
    p["excluded"] = "exclude" in decisions.get(p["key"], set())
    p["site_title"] = sites.SITES.get(p["site"], {}).get("title", p["site"])
    return p


def new_sample(con, settings, n=30, seed=None) -> list[str]:
    """Новая выборка: ключи товаров. Основит — до трети, остальное поровну по сетям."""
    now = time.time()
    rng = random.Random(seed)
    recent = set(db.spot_latest(con, now - RECHECK_AFTER))
    decisions = db.decisions(con)
    brand = settings.get("brand_words") or []
    rows = [
        dict(r)
        for r in con.execute(
            "SELECT key, site, name, vendor FROM products WHERE price IS NOT NULL AND last_seen>=? ORDER BY key",
            (now - 7 * 86400,),
        )
    ]
    pool = [r for r in rows if r["key"] not in recent and "exclude" not in decisions.get(r["key"], set())]
    if len(pool) < n:  # мало непроверенных — берём и проверенные давно
        pool = [r for r in rows if "exclude" not in decisions.get(r["key"], set())]
    ours = [r for r in pool if analysis.is_ours(r.get("name"), r.get("vendor"), brand)[0]]
    others = [r for r in pool if r not in ours]
    rng.shuffle(ours)
    picked = ours[: n // 3]
    by_site: dict[str, list[dict[str, Any]]] = {}
    for r in others:
        by_site.setdefault(r["site"], []).append(r)
    for lst in by_site.values():
        rng.shuffle(lst)
    # по очереди из каждой сети, пока не наберём n
    while len(picked) < n and any(by_site.values()):
        for site in sorted(by_site):
            if by_site[site] and len(picked) < n:
                picked.append(by_site[site].pop())
    if len(picked) < n:  # товаров Основит больше, чем остальных
        picked += [r for r in ours if r not in picked][: n - len(picked)]
    keys = [r["key"] for r in picked]
    db.kv_set(con, SAMPLE_KEY, {"keys": keys, "ts": now})
    return keys


def stats(con, since=None) -> dict[str, Any]:
    since = since if since is not None else time.time() - WINDOW
    latest = db.spot_latest(con, since)
    total = len(latest)
    by = {v: 0 for v in VERDICTS}
    for r in latest.values():
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    return {
        "total": total,
        "ok": by.get("ok", 0),
        "accuracy": round(by.get("ok", 0) / total * 100) if total else None,
        "by": by,
        "labels": VERDICTS,
    }


def view(con, settings) -> dict[str, Any]:
    sample = db.kv_get(con, SAMPLE_KEY) or {}
    keys, since = sample.get("keys") or [], sample.get("ts") or 0
    checks = db.spot_latest(con, since) if since else {}
    kind_ov, pack_ov, decisions = db.kind_overrides(con), db.pack_overrides(con), db.decisions(con)
    brand = settings.get("brand_words") or []
    items = []
    for key in keys:
        p = _row(con, key)
        if not p:
            continue
        enrich(p, kind_ov, pack_ov, decisions, brand)
        p["check"] = checks.get(key)
        items.append(p)
    return {"items": items, "sample_ts": since, "stats": stats(con)}


def mark(con, settings, key, verdict, value="") -> dict[str, Any]:
    """Отметка проверки. Ошибка фасовки и вида сразу исправляется ручной правкой."""
    if verdict not in VERDICTS:
        raise ValueError(f"неизвестная отметка: {verdict}")
    p = _row(con, key)
    if not p:
        raise ValueError("товар не найден")
    kind_ov, pack_ov, decisions = db.kind_overrides(con), db.pack_overrides(con), db.decisions(con)
    shown = enrich(dict(p), kind_ov, pack_ov, decisions, settings.get("brand_words") or [])
    value = " ".join((value or "").split())
    if verdict == "pack" and value:
        db.set_pack(con, key, value)  # ValueError, если фасовку не понять
    elif verdict == "kind" and value:
        db.set_kind(con, key, value)
    elif verdict == "not_competitor":
        db.decide(con, key, "exclude", "выборочная проверка")
    db.spot_add(con, key, verdict, value, {"price": shown["price"], "pack": shown["pack"], "kind": shown["kind"]})
    return {"ok": True}


def undo(con, key) -> dict[str, Any]:
    """Снять отметку в текущей выборке (ручные правки остаются — их можно вернуть в карточке товара)."""
    sample = db.kv_get(con, SAMPLE_KEY) or {}
    db.spot_undo(con, key, sample.get("ts") or 0)
    return {"ok": True}
