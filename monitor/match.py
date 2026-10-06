"""Сопоставление товаров с полки (выдача раздела на сайте) с фидом и решение, открывать ли карточку.

Стратегия сбора с сайта «сначала выдача, карточки — только когда нужно»:
  1. shallow — страницы разделов (с пагинацией): название, цена, ссылка, наличие, город;
  2. матчинг с фидом — по артикулу (из ссылки или данных страницы), иначе по названию и фасовке;
  3. решение о deep-заходе в карточку (deep_reasons):
       • фасовку не понять ни из названия на полке, ни из фида → «фасовка»;
       • в выдаче цена «от …» → «цена от»;
       • цена на полке расходится с фидом больше порога (crosscheck.warn_pct, 10 %) → «расходится с фидом»;
       • вид товара не распознан, а он нужен для аналогов (товар Основит или название в выдаче обрезано) → «вид»;
       • товар нужен только для «полка против фида» и цена совпала → карточку не открываем;
  4. deep — только отобранные карточки, с тем же лимитом страниц и паузами (visitor.run_site).
"""

from __future__ import annotations

import re
from typing import Any

from . import analysis, kinds, sites, units

PACK = "фасовка"
PRICE_FROM = "цена от"
PRICE_GAP = "расходится с фидом"
KIND = "вид"
# порядок важности: при лимите страниц сначала то, что меняет цену в отчёте
PRIORITY = (PRICE_GAP, PRICE_FROM, PACK, KIND)

_TRUNCATED = re.compile(r"(…|\.\.\.)\s*$")
_WORD = re.compile(r"[a-zа-я0-9]+(?:[.,][0-9]+)?", re.I)


def truncated(name: str | None) -> bool:
    """Название в выдаче обрезано («Штукатурка гипсовая Основит Гипсв…»)."""
    return bool(_TRUNCATED.search(name or ""))


def tokens(name: str | None) -> frozenset[str]:
    """Слова названия без регистра, «ё», знаков и обрезанного последнего слова."""
    text = (name or "").lower().replace("ё", "е")
    cut = truncated(text)
    text = _TRUNCATED.sub("", text)
    words = [w.replace(",", ".") for w in _WORD.findall(text)]
    if cut and words:
        words = words[:-1]  # «Гипсв…» — не слово
    return frozenset(words)


def pack(name: str | None, params: dict | None = None) -> tuple[float | None, str | None]:
    return units.pack_of(name, params, kinds.pack_unit(name))


class FeedIndex:
    """Товары фида одной сети: поиск по ключу (артикулу) и по названию с фасовкой."""

    MIN_WORDS = 3  # по обрезанному названию сопоставляем, только если осталось хотя бы 3 слова

    def __init__(self, site: str, found: dict[str, dict[str, Any]]):
        self.site = site
        self.items = {k: o for k, o in found.items() if o.get("source") == "feed"}
        self.by_tokens: dict[frozenset[str], list[str]] = {}
        self.entries: list[tuple[str, frozenset[str], tuple[float | None, str | None]]] = []
        for k, o in self.items.items():
            t = tokens(o.get("name"))
            if not t:
                continue
            self.by_tokens.setdefault(t, []).append(k)
            self.entries.append((k, t, pack(o.get("name"), o.get("params"))))

    def match(self, item: dict[str, Any]) -> tuple[str | None, str | None]:
        """(ключ товара фида, как сопоставили) или (None, None)."""
        key = sites.product_key(self.site, code=item.get("code"), url=item.get("url"))
        if key and key in self.items:
            return key, "артикул"
        t = tokens(item.get("name"))
        if not t:
            return None, None
        same = self.by_tokens.get(t) or []
        if len(same) == 1:
            return same[0], "название"
        if len(t) < self.MIN_WORDS:
            return None, None
        # название в выдаче короче или обрезано: все его слова есть в названии фида и фасовка та же
        qty, unit = pack(item.get("name"))
        cands = [
            k
            for k, ft, (fq, fu) in self.entries
            if t <= ft and (qty is None or (fq is not None and fu == unit and abs(fq - qty) < 1e-6))
        ]
        if len(cands) == 1:
            return cands[0], "название и фасовка"
        return None, None


def deep_reasons(
    item: dict[str, Any], feed_item: dict[str, Any] | None, warn_pct: float, brand: list[str]
) -> list[str]:
    """Зачем открывать карточку товара с полки; пустой список — не нужно."""
    out = []
    price = item.get("price")
    if item.get("price_from"):
        out.append(PRICE_FROM)  # «от …» с фидом не сравниваем: это цена другой фасовки
    elif (
        feed_item
        and price
        and feed_item.get("price")
        and abs(price - feed_item["price"]) / feed_item["price"] * 100 > warn_pct
    ):
        out.append(PRICE_GAP)
    name = item.get("name") or ""
    qty, _ = pack(name, item.get("params"))
    if not qty and not (feed_item and pack(feed_item.get("name"), feed_item.get("params"))[0]):
        out.append(PACK)
    known = kinds.attrs(name) or (feed_item and kinds.attrs(feed_item.get("name")))
    if not known:
        ours = analysis.is_ours(name, item.get("vendor") or (feed_item or {}).get("vendor"), brand)[0]
        if ours or truncated(name):
            out.append(KIND)
    return out


def order(targets: list[tuple[dict[str, Any], list[str]]], brand: list[str]) -> list[tuple[dict[str, Any], list[str]]]:
    """Сначала Основит, затем по важности причины: при лимите страниц важное не останется за бортом."""

    def rank(t):
        item, reasons = t
        ours = analysis.is_ours(item.get("name"), item.get("vendor"), brand)[0]
        return (not ours, min(PRIORITY.index(r) for r in reasons))

    return sorted(targets, key=rank)


def merge_card(item: dict[str, Any], card: dict[str, Any]) -> None:
    """Карточка уточняет товар с полки: её цена точнее «от …», её название полнее, её характеристики — фасовка."""
    if card.get("price"):
        item["price"] = card["price"]
        item["old_price"] = card.get("old_price")
        item["price_from"] = False
    if card.get("available") is not None:
        item["available"] = card["available"]
    if len(card.get("name") or "") > len(item.get("name") or "") or truncated(item.get("name")):
        item["name"] = card.get("name") or item.get("name")
    if card.get("params"):
        item["params"] = {**(item.get("params") or {}), **card["params"]}
    item["code"] = item.get("code") or card.get("code")
    item["vendor"] = item.get("vendor") or card.get("vendor") or ""
