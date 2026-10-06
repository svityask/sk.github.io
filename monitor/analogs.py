"""Автопоиск аналогов: после основного сбора — по всему фиду сети, а не только по отслеживаемым категориям.

Фид — это весь каталог сети, он уже скачан, поэтому поиск ничего не стоит: ни одного лишнего захода на сайт.
  1. Товары Основит находятся по всему фиду (по словам бренда), даже если их категория не отслеживается.
  2. Виды товаров Основит (штукатурка гипсовая, затирка, клей плиточный C1…) задают, что искать у конкурентов.
  3. Аналог — товар того же вида и той же единицы (кг/л) с понятной фасовкой и ценой. Сначала «настоящие» аналоги
     (не противоречит ни один признак: основа, финиш/старт, класс клея, марка), затем — того же вида; в наличии —
     раньше, чем «нет в наличии». На каждый вид — не больше max_per_kind (настройка), чтобы отчёт оставался читаемым.
"""

from __future__ import annotations

from typing import Any

from . import analysis, kinds, match

AUTO = "auto"  # products.found_by: найден автопоиском; 'tracked' — через «Что отслеживаем»


def wanted(offer: dict[str, Any], types: set[str] | None) -> bool:
    """Нужен ли товар фида как кандидат: Основит или товар знакомого вида, с ценой в рублях."""
    if offer.get("price") is None or offer.get("currency") not in (None, "RUB"):
        return False
    if not kinds.maybe_kind(offer.get("name")):
        return False  # большая часть каталога отсеивается здесь, без полного разбора названия
    a = kinds.attrs(offer.get("name"))
    return bool(a) and (not types or a["type"] in types)


def pick(
    candidates: list[dict[str, Any]],
    tracked: dict[str, dict[str, Any]],
    brand: list[str],
    max_per_kind: int = 40,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Из кандидатов фида — товары Основит и их аналоги, которых ещё нет среди отслеживаемых.

    Возвращает (товары, сводка). Сводка: {'ours': n, 'kinds': {вид: {'found': n, 'taken': n}}}.
    """
    pool = {c["key"]: c for c in candidates if c.get("key") and c["key"] not in tracked}

    def is_ours(o):
        return analysis.is_ours(o.get("name"), o.get("vendor"), brand)[0]

    ours_all = [o for o in list(tracked.values()) + list(pool.values()) if is_ours(o)]
    # виды Основит и единицы, в которых их продают: аналог должен сравниваться за ту же единицу
    our_kinds: dict[str, list[dict[str, Any]]] = {}
    our_units: dict[str, set[str]] = {}
    for o in ours_all:
        a = kinds.attrs(o.get("name"))
        _qty, unit = match.pack(o.get("name"), o.get("params"))
        if a and unit:
            our_kinds.setdefault(a["type"], []).append(a)
            our_units.setdefault(a["type"], set()).add(unit)
    out = [o for o in pool.values() if is_ours(o) and kinds.attrs(o.get("name")).get("type") in our_kinds]
    summary: dict[str, Any] = {"ours": len(out), "kinds": {}}
    by_type: dict[str, list[tuple[tuple, dict[str, Any]]]] = {}
    for c in pool.values():
        if is_ours(c):
            continue
        a = kinds.attrs(c.get("name"))
        t = a.get("type")
        if t not in our_kinds:
            continue
        qty, unit = match.pack(c.get("name"), c.get("params"))
        if not qty or unit not in our_units[t]:
            continue  # без фасовки цену за кг/л не посчитать — такой «аналог» только шумит в «На проверку»
        rank = (
            not any(kinds.compatible(oa, a) for oa in our_kinds[t]),  # сначала настоящие аналоги
            c.get("available") is False,  # в наличии — раньше
            c.get("name") or "",
        )
        by_type.setdefault(t, []).append((rank, c))
    for t, lst in sorted(by_type.items()):
        lst.sort(key=lambda x: x[0])
        taken = [c for _, c in lst[:max_per_kind]]
        out += taken
        summary["kinds"][t] = {"found": len(lst), "taken": len(taken)}
    return out, summary
