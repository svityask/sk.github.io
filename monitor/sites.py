"""Сети: адреса, вид ссылок, где взять артикул и город.

Площадка описывается здесь один раз; остальной код знает только ключ ('petrovich', 'lemanapro').
"""

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

SITES: dict[str, dict[str, Any]] = {
    "petrovich": {
        "title": "Петрович",
        "domains": ("petrovich.ru",),
        "home": "https://petrovich.ru/",
        "data_hosts": ("petrovich.ru",),  # откуда страница сама берёт данные
        # город задаётся поддоменом
        "subdomain_city": {"": "Санкт-Петербург", "www": "Санкт-Петербург", "moscow": "Москва"},
    },
    "lemanapro": {
        "title": "Лемана ПРО",
        "domains": ("lemanapro.ru", "leroymerlin.ru"),
        "home": "https://lemanapro.ru/",
        "data_hosts": ("lemanapro.ru", "api-lmn.ru"),
        "subdomain_city": {},
    },
}

# Партнёрские сети заворачивают ссылку на товар в свою: настоящий адрес лежит в параметре.
_WRAP_PARAMS = ("ulp", "url", "u", "to", "redirect", "redirect_url", "target", "dl")


def unwrap_link(url):
    """Достаёт настоящий адрес товара из партнёрской ссылки (Admitad ulp=, «Где Слон?» и т. п.)."""
    if not url:
        return ""
    url = url.strip()
    for _ in range(3):  # бывает двойная обёртка
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if site_of(url):
            break
        qs = parse_qs(parts.query)
        inner = None
        for key in _WRAP_PARAMS:
            for value in qs.get(key, []):
                value = unquote(value)
                if value.startswith(("http://", "https://")) and site_of(value):
                    inner = value
                    break
            if inner:
                break
        if not inner or not host:
            break
        url = inner
    return url


def site_of(url):
    """Ключ сети по адресу или None."""
    host = urlsplit(url if "//" in (url or "") else "https://" + (url or "")).netloc.lower().split(":")[0]
    for key, site in SITES.items():
        for dom in site["domains"]:
            if host == dom or host.endswith("." + dom):
                return key
    return None


def normalize_url(url):
    """Адрес товара без меток и якорей; leroymerlin.ru → lemanapro.ru; https и косая черта в конце."""
    url = unwrap_link(url)
    if not url:
        return ""
    if "//" not in url:
        url = "https://" + url
    p = urlsplit(url)
    host = p.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    host = host.replace("leroymerlin.ru", "lemanapro.ru")
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return urlunsplit(("https", host, path, "", ""))


def code_from_url(url):
    """Артикул товара из адреса карточки или None (раздел, главная)."""
    u = normalize_url(url)
    key = site_of(u)
    path = urlsplit(u).path
    if key == "petrovich":
        m = re.search(r"/product/(\d{3,})/", path) or re.search(r"/catalog/(?:[^/]+/)+(\d{4,})/$", path)
        return m.group(1) if m else None
    if key == "lemanapro":
        m = re.search(r"/product/[^/]*?-(\d{5,})/$", path) or re.search(r"/product/(\d{5,})/$", path)
        return m.group(1) if m else None
    return None


def is_product_url(url):
    return code_from_url(url) is not None


def is_section_url(url):
    u = normalize_url(url)
    key = site_of(u)
    if not key or is_product_url(u):
        return False
    path = urlsplit(u).path
    if key == "petrovich":
        return path.startswith("/catalog/")
    return path.startswith(("/catalogue/", "/catalog/"))


def city_from_url(url):
    """Город, который задаёт сам адрес (только Петрович), иначе None."""
    u = normalize_url(url)
    key = site_of(u)
    sub_map = SITES.get(key, {}).get("subdomain_city") or {}
    if not sub_map:
        return None
    host = urlsplit(u).netloc
    sub = host[: -len("petrovich.ru")].rstrip(".")
    return sub_map.get(sub)


def origin(url):
    p = urlsplit(normalize_url(url))
    return f"https://{p.netloc}"


def product_key(site, code=None, url=None, fallback_id=None):
    """Единый ключ товара: одинаковый для фида и для окна Edge."""
    c = code or (code_from_url(url) if url else None) or (str(fallback_id).strip() if fallback_id else None)
    return f"{site}:{c}" if c else None
