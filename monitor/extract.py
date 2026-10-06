"""Разбор страницы сети без сети: товары ищутся по смыслу, а не по точным именам полей.

Порядок доверия: данные, которые страница получила сама (JSON) → разметка schema.org → состояние в
скриптах → вёрстка. Сеть может переименовать поля без предупреждения — поэтому товар здесь —
это объект, где рядом лежат название, цена и артикул или ссылка.
"""

import json
import re

from . import sites
from .feeds import parse_number

# Скрипт выполняется в странице и возвращает всё, что нужно разбору, одним объектом.
PAGE_SCRIPT = r"""
(() => {
  const out = {url: location.href, title: document.title || '', jsonld: [], state: [], cards: [], city: '', next: '', text: ''};
  out.text = (document.body ? document.body.innerText : '').slice(0, 4000);
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { out.jsonld.push(JSON.parse(s.textContent)); } catch (e) {}
  }
  for (const s of document.querySelectorAll('script')) {
    const t = s.textContent || '';
    if (s.type === 'application/json' || s.id === '__NEXT_DATA__' || s.id === '__NUXT_DATA__') {
      if (t.length < 6e6) out.state.push(t);
      continue;
    }
    const m = t.match(/^\s*(?:window\.)?(__[A-Z_]+__|__NUXT__|INITIAL_STATE)\s*=\s*(\{[\s\S]*\})\s*;?\s*$/);
    if (m && m[2].length < 6e6) out.state.push(m[2]);
  }
  // вёрстка: ссылки на карточки и ближайший к ним контейнер с ценой
  const priceRe = /(\d[\d\s  ]{0,9}(?:[.,]\d{1,2})?)\s*(?:₽|руб)/;
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/product/"], a[href*="/catalog/"]')) {
    const href = a.href.split('#')[0].split('?')[0];
    if (seen.has(href) || !/\/product\/|\/catalog\/.+\/\d{4,}\/?$/.test(href)) continue;
    let box = a, found = null;
    for (let i = 0; i < 7 && box; i++, box = box.parentElement) {
      if (box.innerText && priceRe.test(box.innerText)) { found = box; break; }
    }
    if (!found) continue;
    seen.add(href);
    const prices = [];
    let old = null;
    for (const el of found.querySelectorAll('*')) {
      if (el.children.length) continue;
      const tx = (el.innerText || '').trim();
      const pm = tx.match(priceRe) || ((/₽|руб/.test((el.parentElement || el).innerText || '')) && tx.match(/^(\d[\d\s  ]{0,9}(?:[.,]\d{1,2})?)$/));
      if (!pm) continue;
      const deco = getComputedStyle(el).textDecorationLine + ' ' + getComputedStyle(el.parentElement || el).textDecorationLine;
      const cls = ((el.className || '') + ' ' + ((el.parentElement || {}).className || '')).toString().toLowerCase();
      if (/line-through/.test(deco) || /old|cross|strike|prev/.test(cls)) { old = old || pm[1]; continue; }
      if (/card|club|gold|loyal|bonus|pro/.test(cls)) continue;  // цена по карте — не цена на полке
      prices.push(pm[1]);
    }
    let name = (a.getAttribute('title') || '').trim();
    if (!name) {
      for (const el of found.querySelectorAll('a[href], [itemprop="name"], h2, h3, [class*="name"], [class*="title"]')) {
        const tx = (el.innerText || '').trim();
        if (tx.length > name.length && tx.length < 300 && !priceRe.test(tx)) name = tx;
      }
    }
    const avail = /нет в наличии|под заказ|закончил/i.test(found.innerText) ? false : null;
    out.cards.push({url: href, name, price: prices[0] || null, old_price: old, available: avail});
  }
  // город в шапке
  const citySel = ['[data-qa*="region"]', '[data-testid*="region"]', '[data-test*="city"]', '[class*="region"]',
                   '[class*="city"]', '[class*="location"]', 'header'];
  for (const sel of citySel) {
    const el = document.querySelector(sel);
    const tx = el && (el.innerText || '').trim();
    if (tx) { out.city = tx.slice(0, 200); break; }
  }
  // следующая страница выдачи
  const rel = document.querySelector('a[rel="next"], link[rel="next"]');
  if (rel && rel.href) out.next = rel.href;
  if (!out.next) {
    for (const a of document.querySelectorAll('a[href]')) {
      const tx = (a.innerText || a.getAttribute('aria-label') || '').trim().toLowerCase();
      if (/^(следующая|далее|вперед|вперёд|›|»|→)$/.test(tx) || /следующая страница/.test(tx)) { out.next = a.href; break; }
    }
  }
  return out;
})()
"""

CHECK_WORDS = (
    "проверка браузера",
    "проверяем ваш браузер",
    "подтвердите, что вы не робот",
    "вы не робот",
    "captcha",
    "капча",
    "qrator",
    "доступ ограничен",
    "access denied",
    "checking your browser",
    "please wait while we",
    "запрос отклонён",
    "слишком много запросов",
    "too many requests",
)

KNOWN_CITIES = (
    "Москва",
    "Санкт-Петербург",
    "Екатеринбург",
    "Новосибирск",
    "Казань",
    "Нижний Новгород",
    "Краснодар",
    "Самара",
    "Ростов-на-Дону",
    "Уфа",
    "Челябинск",
    "Пермь",
    "Воронеж",
    "Волгоград",
    "Красноярск",
    "Омск",
    "Тюмень",
    "Тула",
    "Ярославль",
    "Калининград",
)


def is_check_page(page, status=None):
    """Похоже на проверку браузера / отказ, а не на каталог."""
    if status in (401, 403, 429, 503):
        return True
    text = ((page or {}).get("title", "") + " " + (page or {}).get("text", "")[:1500]).lower()
    return any(w in text for w in CHECK_WORDS) and len((page or {}).get("cards") or []) == 0


def city_of(page):
    text = (page or {}).get("city") or ""
    for c in KNOWN_CITIES:
        if c.lower() in text.lower():
            return c
    if "петербург" in text.lower() or "спб" in text.lower():
        return "Санкт-Петербург"
    return None


def same_city(a, b):
    if not a or not b:
        return True

    def norm(s):
        return s.lower().replace("ё", "е").replace("г.", "").replace("спб", "санкт-петербург").strip()

    return norm(a) in norm(b) or norm(b) in norm(a)


# ---------------------------------------------------------------- поиск товаров в данных

_NAME_KEYS = ("name", "title", "displayName", "productName", "fullName", "label", "shortName")
_CODE_KEYS = ("code", "sku", "article", "articul", "productId", "product_id", "vendorCode", "itemId", "id", "plu")
_URL_KEYS = ("url", "link", "href", "productUrl", "canonicalUrl", "slug", "path")
_SHELF_KEYS = (
    "retail",
    "displayMain",
    "main",
    "current",
    "regular",
    "base",
    "price",
    "value",
    "amount",
    "final",
    "sale",
)
_OLD_KEYS = ("displayOld", "old", "oldPrice", "old_price", "previous", "crossed", "priceOld", "strikethrough", "before")
_CARD_HINT = ("gold", "club", "card", "loyal", "bonus", "pro", "partner", "member")


def _price_from(value):
    """(цена на полке, старая цена) из числа, строки или вложенного объекта цены."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value) if value > 0 else None), None
    if isinstance(value, str):
        return parse_number(value), None
    if isinstance(value, dict):
        shelf = old = None
        for k in _SHELF_KEYS:
            if k in value and not any(h in k.lower() for h in _CARD_HINT):
                shelf, _ = _price_from(value[k])
                if shelf:
                    break
        for k in _OLD_KEYS:
            if k in value:
                old, _ = _price_from(value[k])
                if old:
                    break
        return shelf, old
    return None, None


def _find_price(obj):
    shelf = old = None
    for k, v in obj.items():
        kl = k.lower()
        if "price" not in kl and kl not in ("prices", "cost"):
            continue
        if any(h in kl for h in _CARD_HINT):
            continue
        if any(o.lower() == kl for o in _OLD_KEYS) or "old" in kl:
            if old is None:
                old, _ = _price_from(v)
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            v = v[0]
        p, o = _price_from(v)
        if p and shelf is None:
            shelf = p
        if o and old is None:
            old = o
    if shelf is None and isinstance(obj.get("offers"), dict):
        shelf, _ = _price_from(obj["offers"].get("price") or obj["offers"].get("lowPrice"))
    return shelf, old


_CONTAINS = {
    _NAME_KEYS: ("name", "title"),
    _CODE_KEYS: ("code", "sku", "article", "articul"),
    _URL_KEYS: ("url", "link", "href"),
}


def _first(obj, keys):
    """Сначала точные имена полей, потом похожие («productLmCode», «productLink», «displayedName»)."""

    def ok(v):
        return isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()

    for k in keys:
        if ok(obj.get(k)):
            return str(obj[k]).strip()
    for part in _CONTAINS.get(keys, ()):
        for k, v in obj.items():
            kl = k.lower()
            if part in kl and ok(v) and not any(x in kl for x in ("image", "img", "photo", "brand", "category", "seo")):
                return str(v).strip()
    return None


def _availability(obj):
    for k in ("available", "inStock", "isAvailable", "availability", "stock"):
        if k in obj:
            v = obj[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v > 0
            if isinstance(v, str):
                s = v.lower()
                if "instock" in s or s in ("true", "in_stock", "available", "в наличии"):
                    return True
                if "outofstock" in s or s in ("false", "out_of_stock", "нет в наличии"):
                    return False
            if isinstance(v, dict):
                return _availability(v)
    offers = obj.get("offers")
    if isinstance(offers, dict):
        return _availability(offers)
    return None


def find_products(data, site, base_url, via):
    """Все объекты-товары в произвольном JSON."""
    found = []

    def walk(o, depth=0):
        if depth > 40:
            return
        if isinstance(o, dict):
            name = _first(o, _NAME_KEYS)
            if name and 4 <= len(name) <= 300:
                price, old = _find_price(o)
                if price:
                    url = _first(o, _URL_KEYS)
                    if url and not url.startswith("http"):
                        url = sites.origin(base_url) + "/" + url.lstrip("/") if "/" in url else None
                    code = sites.code_from_url(url) if url else None
                    code = code or _first(o, _CODE_KEYS)
                    if code and not url and not re.search(r"\d{4,}", code):
                        code = None  # «id: 3» у способа доставки — не артикул
                    if code or url:
                        found.append(
                            {
                                "site": site,
                                "code": code,
                                "name": name,
                                "price": price,
                                "old_price": old,
                                "url": sites.normalize_url(url) if url and sites.site_of(url) == site else "",
                                "available": _availability(o),
                                "via": via,
                                "vendor": _brand(o),
                            }
                        )
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    walk(data)
    return found


def _brand(o):
    b = o.get("brand") or o.get("vendor") or o.get("manufacturer")
    if isinstance(b, dict):
        b = b.get("name") or b.get("title")
    return b.strip() if isinstance(b, str) else ""


def _from_jsonld(blocks, site, base_url):
    out = []
    for b in blocks:
        items = b if isinstance(b, list) else (b.get("@graph") if isinstance(b, dict) and "@graph" in b else [b])
        for it in items or []:
            if not isinstance(it, dict):
                continue
            t = it.get("@type")
            types = t if isinstance(t, list) else [t]
            if "ItemList" in types:
                for el in it.get("itemListElement") or []:
                    if isinstance(el, dict):
                        sub = el.get("item") if isinstance(el.get("item"), dict) else el
                        out += find_products(sub, site, base_url, "разметка")
            elif "Product" in types:
                offers = it.get("offers")
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    price = parse_number(str(offers.get("price") or offers.get("lowPrice") or ""))
                    if price:
                        url = it.get("url") or offers.get("url") or base_url
                        code = sites.code_from_url(url) or (str(it.get("sku") or it.get("productID") or "") or None)
                        out.append(
                            {
                                "site": site,
                                "code": code,
                                "name": " ".join(str(it.get("name") or "").split()),
                                "price": price,
                                "old_price": None,
                                "url": sites.normalize_url(url),
                                "available": _availability(offers),
                                "via": "разметка",
                                "vendor": _brand(it),
                            }
                        )
    return out


def products_from_page(page, network_json, site):
    """Товары страницы: (список, каким путём прочитаны)."""
    base = page.get("url") or sites.SITES[site]["home"]
    layers = []
    api = []
    for _, data in network_json or []:
        api += find_products(data, site, base, "данные страницы")
    layers.append(api)
    layers.append(_from_jsonld(page.get("jsonld") or [], site, base))
    state = []
    for text in page.get("state") or []:
        try:
            state += find_products(json.loads(text), site, base, "состояние страницы")
        except ValueError:
            continue
    layers.append(state)
    cards = []
    for c in page.get("cards") or []:
        price = parse_number(c.get("price"))
        if not price or sites.site_of(c.get("url", "")) != site:
            continue
        cards.append(
            {
                "site": site,
                "code": sites.code_from_url(c["url"]),
                "name": " ".join((c.get("name") or "").split()),
                "price": price,
                "old_price": parse_number(c.get("old_price")),
                "url": sites.normalize_url(c["url"]),
                "available": c.get("available"),
                "via": "вёрстка",
                "vendor": "",
            }
        )
    layers.append(cards)

    merged: dict[str, dict] = {}
    order: list[str] = []
    for layer in layers:
        for p in layer:
            k = p.get("code") or p.get("url")
            if not k:
                continue
            if k in merged:
                cur = merged[k]
                for f in ("url", "old_price", "vendor", "available"):  # дополняем, не перезаписываем
                    if cur.get(f) in (None, "") and p.get(f) not in (None, ""):
                        cur[f] = p[f]
                continue
            merged[k] = dict(p)
            order.append(k)
    items = [merged[k] for k in order]
    for p in items:
        if p.get("old_price") and p["old_price"] <= p["price"]:
            p["old_price"] = None
    vias = sorted({p["via"] for p in items})
    return items, ", ".join(vias) if vias else "ничего"
