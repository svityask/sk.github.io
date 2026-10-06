"""Сравнение с рынком и правило «не уверены — на проверку со ссылкой»."""

import statistics

from . import kinds, units

SOURCE_TITLE = {"feed": "фид", "edge": "сайт (Edge)"}


def is_ours(name, vendor, brand_words):
    """(наш?, сомнение?) — сомнение, если бренд есть только в названии, а производитель другой."""
    words = [w.lower() for w in brand_words if w.strip()]
    in_vendor = any(w in (vendor or "").lower() for w in words)
    in_name = any(w in (name or "").lower() for w in words)
    if in_vendor:
        return True, False
    if in_name and vendor:
        return False, True
    return in_name, False


def _median(values):
    return statistics.median(values) if values else None


LEVELS = ("аналоги", "тот же тип", "раздел")
MIN_POOL = 3


def analyse(products, groups, settings, decisions, missing=(), overrides=None):
    """products — товары этого сбора (строки таблицы products).
    groups — {group_id: title}. missing — [(site, ref, title)] отслеживаемые товары, которых нет.
    overrides — {key: вид}, вид товара, поправленный вручную.

    Основит сравнивается с аналогами: тот же тип и ни одного противоречащего признака (основа, финиш/старт,
    класс клея, марка). Аналогов нет — с тем же типом; вид не распознан — с разделом.

    Возвращает (market_rows, review_items, kind_stats).
    """
    overrides = overrides or {}
    rules = settings["review"]
    ratio = float(rules.get("unit_ratio") or 3.0)
    jump = float(rules.get("jump_pct") or 40) / 100
    review = []

    def flag(p, kind, reason, signature=None):
        sig = f"{kind}:{signature}" if signature is not None else kind
        if f"accept:{sig}" in decisions.get(p["key"], set()):
            return
        review.append(
            {
                "key": p["key"],
                "site": p["site"],
                "name": p.get("name") or p["key"],
                "url": p.get("url") or "",
                "price": p.get("price"),
                "reason": reason,
                "signature": sig,
                "kind": kind,
            }
        )

    live = [p for p in products if "exclude" not in decisions.get(p["key"], set())]

    for p in live:
        p["per_unit"] = units.per_unit(p.get("price"), p.get("pack_qty"))
        p["attrs"], p["kind_manual"] = kinds.attrs_of(p, overrides)
        p["kind"] = kinds.label(p["attrs"]) or ""
        if not p.get("pack_qty"):
            flag(p, "pack", "Не распознана фасовка — цену за кг/л не посчитать")
        if p.get("brand_doubt"):
            flag(p, "brand", "«Основит» есть в названии, но производитель другой — наш ли это товар?")
        if p.get("is_ours") and not p["kind"]:
            flag(
                p,
                "kind",
                "Вид товара не распознан — Основит сравнивается со всем разделом. Укажите вид во вкладке «Товары»",
            )
        prev = p.get("prev_price") if p.get("changed_now") else None  # только изменение в этом сборе
        if prev and p.get("price") and abs(p["price"] - prev) / prev >= jump:
            pct = (p["price"] - prev) / prev * 100
            flag(
                p,
                "jump",
                f"Скачок цены за один сбор: {rub(prev)} → {rub(p['price'])} ₽ ({pct:+.0f} %)",
                round(p["price"]),
            )
        if (
            p.get("prev_source")
            and p.get("source")
            and p["prev_source"] != p["source"]
            and prev
            and p.get("price")
            and abs(p["price"] - prev) / prev >= 0.10
        ):
            flag(
                p,
                "source",
                f"Цена с другого источника ({SOURCE_TITLE.get(p['prev_source'])} → "
                f"{SOURCE_TITLE.get(p['source'])}) отличается на {abs(p['price'] - prev) / prev * 100:.0f} %",
                round(p["price"]),
            )

    # ---- единица измерения раздела
    by_group: dict[tuple[str, int], list[dict]] = {}
    for p in live:
        if p.get("group_id") is not None:
            by_group.setdefault((p["site"], p["group_id"]), []).append(p)
    for (_site, gid), items in by_group.items():
        seen = [p["pack_unit"] for p in (([p for p in items if p.get("is_ours")]) or items) if p.get("pack_unit")]
        if not seen:
            continue
        main = max(set(seen), key=seen.count)
        for p in items:
            if p.get("pack_unit") and p["pack_unit"] != main and not p.get("kind"):
                flag(
                    p,
                    "unit",
                    f"Другая единица измерения: {p['pack_unit']} вместо {main} в разделе «{groups.get(gid, '—')}»",
                )

    comp = [p for p in live if not p.get("is_ours") and p.get("per_unit")]

    def pools(o, among):
        """Кандидаты для сравнения по уровням: аналоги → тот же тип → раздел."""
        same = [c for c in among if c is not o and c["site"] == o["site"] and c.get("pack_unit") == o.get("pack_unit")]
        a = o.get("attrs") or {}
        if a:  # вид известен: со всем разделом не сравниваем — там другие товары
            return [
                [c for c in same if kinds.compatible(a, c.get("attrs"))],
                [c for c in same if (c.get("attrs") or {}).get("type") == a.get("type")],
                [],
            ]
        return [[], [], [c for c in same if c.get("group_id") == o.get("group_id") and o.get("group_id") is not None]]

    # ---- выбросы: цена за единицу в N раз от середины своих аналогов (или раздела)
    clean = []
    for c in comp:
        ref = next((pl for pl in pools(c, comp) if len(pl) >= MIN_POOL), None)
        med = _median([x["per_unit"] for x in ref]) if ref else None
        if med and (c["per_unit"] >= med * ratio or c["per_unit"] <= med / ratio):
            side = "выше" if c["per_unit"] > med else "ниже"
            k = c["per_unit"] / med if c["per_unit"] > med else med / c["per_unit"]
            flag(
                c,
                "ratio",
                f"Цена за {c['pack_unit']} в {str(round(k, 1)).replace('.', ',')} раза {side} "
                f"середины похожих товаров — возможно, не та фасовка или не тот товар",
            )
        else:
            clean.append(c)

    # ---- Основит против рынка
    market = []
    for o in [p for p in live if p.get("is_ours")]:
        row = {
            "key": o["key"],
            "site": o["site"],
            "group": groups.get(o.get("group_id"), "—"),
            "kind": o["kind"],
            "kind_manual": o["kind_manual"],
            "name": o.get("name"),
            "url": o.get("url"),
            "pack": units.pack_label(o.get("pack_qty"), o.get("pack_unit")),
            "price": o.get("price"),
            "per_unit": o.get("per_unit"),
            "unit": o.get("pack_unit") or "",
            "median": None,
            "count": 0,
            "min": None,
            "min_name": "",
            "min_url": "",
            "deviation": None,
            "cheaper_than": None,
            "level": None,
            "available": o.get("available"),
            "source": SOURCE_TITLE.get(o.get("source"), o.get("source")),
            "analogs": [],
        }
        if o.get("per_unit"):
            # Аналоги, даже если их мало, честнее, чем «тот же тип» с другой основой; тот же тип — только когда
            # аналогов нет совсем (и тогда это видно в отчёте).
            level, pool = next(((lv, pl) for lv, pl in zip(LEVELS, pools(o, clean), strict=True) if pl), (None, []))
            if pool:
                avail = [c for c in pool if c.get("available") is not False]
                base = avail if len(avail) >= MIN_POOL else pool
                med = _median([c["per_unit"] for c in base])
                cheapest = min(base, key=lambda c: c["per_unit"])
                row.update(
                    level=level,
                    median=med,
                    count=len(base),
                    min=cheapest["per_unit"],
                    min_name=cheapest.get("name") or "",
                    min_url=cheapest.get("url") or "",
                    deviation=(o["per_unit"] - med) / med * 100 if med else None,
                    cheaper_than=sum(1 for c in base if c["per_unit"] > o["per_unit"]),
                    analogs=[
                        {
                            "key": c["key"],
                            "name": c.get("name"),
                            "url": c.get("url"),
                            "price": c.get("price"),
                            "per_unit": c["per_unit"],
                            "kind": c.get("kind"),
                            "pack": units.pack_label(c.get("pack_qty"), c.get("pack_unit")),
                            "available": c.get("available"),
                        }
                        for c in sorted(base, key=lambda c: c["per_unit"])[:12]
                    ],
                )
        market.append(row)

    # ---- рынок по видам (для сводки)
    by_kind: dict[tuple[str, str, str], list[float]] = {}
    for c in clean:
        if c.get("kind"):
            by_kind.setdefault((c["site"], c["kind"], c["pack_unit"]), []).append(c["per_unit"])
    stats = [
        {"site": s, "kind": k, "unit": u, "count": len(v), "median": _median(v), "min": min(v)}
        for (s, k, u), v in sorted(by_kind.items())
    ]

    for site, ref, title in missing:
        review.append(
            {
                "key": f"missing:{site}:{ref}",
                "site": site,
                "name": title or ref,
                "url": ref,
                "price": None,
                "reason": "Товар из списка не найден ни в фиде, ни на сайте — снят с продажи или сменилась ссылка",
                "signature": "missing",
                "kind": "missing",
            }
        )
    return market, review, stats


def change_text(name, old, new, site_title, source):
    pct = (new - old) / old * 100 if old else 0
    verb = "подешевел" if new < old else "подорожал"
    pct_s = f"{pct:+.1f}".replace(".", ",").replace("-", "−")
    return f"{name}: {verb} с {rub(old)} до {rub(new)} ₽ ({pct_s} %) — {site_title}, {SOURCE_TITLE.get(source, source)}"


def rub(x):
    """1234.5 → «1 234,50»; целые — без копеек."""
    if x is None:
        return "—"
    s = f"{x:,.2f}".replace(",", "\u00a0").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s
