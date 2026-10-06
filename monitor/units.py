"""Фасовка и цена за кг/л.

Сравнивать мешок 25 кг с мешком 30 кг по цене за штуку нельзя — сравниваем за кг (или за литр).
Если фасовку не удалось понять, товар не угадывается, а идёт «На проверку».
"""

import re

# единица -> (базовая единица, множитель)
_UNITS = {
    "кг": ("кг", 1.0),
    "kg": ("кг", 1.0),
    "килограмм": ("кг", 1.0),
    "килограмма": ("кг", 1.0),
    "килограммов": ("кг", 1.0),
    "г": ("кг", 0.001),
    "гр": ("кг", 0.001),
    "g": ("кг", 0.001),
    "грамм": ("кг", 0.001),
    "грамма": ("кг", 0.001),
    "т": ("кг", 1000.0),
    "тонна": ("кг", 1000.0),
    "л": ("л", 1.0),
    "l": ("л", 1.0),
    "литр": ("л", 1.0),
    "литра": ("л", 1.0),
    "литров": ("л", 1.0),
    "мл": ("л", 0.001),
    "ml": ("л", 0.001),
}

_NUM = r"(\d+(?:[.,]\d+)?)"
# латинские «g» и «l» не берём: «Гипсвелл PC21 G» — это марка, а не 21 грамм
_UNIT_RE = r"(кг|kg|килограмм(?:а|ов)?|гр?|грамм(?:а)?|т|тонна|л|литр(?:а|ов)?|мл|ml)"
# «25 кг», «25кг.», «0,9 л», «2 x 5 кг» (берём произведение). Число не должно прилипать к букве: «PC21» — не фасовка
_PACK_RE = re.compile(r"(?<![\w.,])(?:(\d+)\s*[xх×*]\s*)?" + _NUM + r"\s*" + _UNIT_RE + r"(?![a-zа-яё0-9])", re.I)

_PARAM_PRIORITY = (
    ("фасовка", None),
    ("вес нетто", "кг"),
    ("масса нетто", "кг"),
    ("объем", "л"),
    ("объём", "л"),
    ("вес", "кг"),
    ("масса", "кг"),
)


def _num(s):
    return float(s.replace(",", "."))


def from_text(text, prefer=None):
    """(количество в базовой единице, базовая единица) или (None, None).

    prefer — 'кг' или 'л': если в названии обе («17 л / 28 кг», «15 кг + 5 л»), берём эту.
    """
    if not text:
        return None, None
    found = []
    for m in _PACK_RE.finditer(text):
        mult, num, unit = m.group(1), m.group(2), m.group(3).lower()
        if unit not in _UNITS:
            continue
        # «2023 г.» — это год, а не 2 кг
        if unit in ("г", "гр") and re.fullmatch(r"(19|20)\d\d", num):
            continue
        base, k = _UNITS[unit]
        qty = _num(num) * k * (int(mult) if mult else 1)
        if qty > 0:
            found.append((qty, base))
    if not found:
        return None, None
    if prefer and len({b for _, b in found}) > 1:
        same = [f for f in found if f[1] == prefer]
        if same:
            # из нескольких фасовок одной единицы берём первую: «15 кг + 5 л» → 15 кг
            return same[0]
    # в названии последняя фасовка обычно и есть упаковка («... 30 кг»)
    return found[-1]


def from_params(params):
    """Фасовка из характеристик фида: {'Вес, кг': ('30', ''), 'Фасовка': ('25 кг', '')}."""
    if not params:
        return None, None
    low = {k.lower().replace("ё", "е"): (k, v) for k, v in params.items()}
    for key, default_unit in _PARAM_PRIORITY:
        key = key.replace("ё", "е")
        for name, (_orig, (value, unit)) in low.items():
            if not name.startswith(key):
                continue
            qty, base = from_text(f"{value} {unit}".strip())
            if qty:
                return qty, base
            unit_in_name = re.search(r",\s*(\w+)\s*$", name)
            u = (unit or (unit_in_name.group(1) if unit_in_name else "") or default_unit or "").lower()
            try:
                v = _num(re.sub(r"[^\d.,]", "", value))
            except ValueError:
                continue
            if u in _UNITS and v > 0:
                base, k = _UNITS[u]
                return v * k, base
    return None, None


def pack_of(name, params=None, prefer="кг"):
    """Фасовка товара: сначала название (там упаковка), затем характеристики фида."""
    qty, base = from_text(name, prefer)
    if qty:
        return qty, base
    return from_params(params)


def per_unit(price, qty):
    if price is None or not qty:
        return None
    return round(price / qty, 2)


def pack_label(qty, base):
    if not qty:
        return ""
    if base == "кг" and qty < 1:
        return f"{qty * 1000:g} г"
    if base == "л" and qty < 1:
        return f"{qty * 1000:g} мл"
    return f"{qty:g} {base}"
