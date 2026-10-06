"""Товарные фиды сетей: Admitad «Товары»/«Каталог товаров», «Где Слон?», любой YML, CSV или Google Merchant.

Один запрос за готовым файлом — без браузера и без обхода чего-либо. Файл читается потоком:
каталог DIY-сети — сотни тысяч позиций, в память целиком он не грузится.
"""

import codecs
import csv
import email.utils
import gzip
import hashlib
import io
import itertools
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from . import guard, log, sites

USER_AGENT = "OsnovitPriceMonitor/2.1 (feed reader; one request per update)"
MIN_INTERVAL_H = 6  # сеть обновляет фид раз в 6 часов — чаще ходить незачем


class FeedError(Exception):
    """Ошибка, понятная человеку: текст идёт прямо в интерфейс."""


# ---------------------------------------------------------------- загрузка


def fetch(
    source,
    dest_dir,
    site,
    force=False,
    min_interval_h=MIN_INTERVAL_H,
    timeout=180,
    meter=None,
    sleep=time.sleep,
    attempts=3,
    retry_base=5.0,
    allow_network=True,
):
    """Кладёт свежий файл фида в dest_dir и возвращает (путь, сведения).

    source — ссылка из кабинета партнёрской сети или путь к файлу на диске.
    Повторная загрузка не чаще раза в min_interval_h часов; сервер может ответить «не изменился» (304).
    """
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"{site}.feed")
    meta_path = path + ".json"
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}

    source = (source or "").strip().strip('"')
    if not source:
        raise FeedError("Не указан фид: вставьте ссылку из кабинета партнёрской сети или выберите файл.")

    if not source.lower().startswith(("http://", "https://")):
        if not os.path.exists(source):
            raise FeedError(f"Файл фида не найден: {source}")
        st = os.stat(source)
        if meta.get("source") != source or meta.get("mtime") != st.st_mtime or not os.path.exists(path):
            shutil.copyfile(source, path)
        meta.update(
            source=source,
            mtime=st.st_mtime,
            fetched_at=time.time(),
            size=os.path.getsize(path),
            status="файл",
            stale=False,  # прошлая неудачная загрузка по ссылке к файлу на диске не относится
        )
        _save_json(meta_path, meta)
        return path, meta

    fresh = (
        meta.get("source") == source
        and os.path.exists(path)
        and time.time() - meta.get("fetched_at", 0) < min_interval_h * 3600
    )
    if fresh and not force:
        meta["status"] = "свежий, не скачивали"
        return path, meta
    if not allow_network:  # источник на паузе: работаем с прошлым файлом, если он есть
        if meta.get("source") == source and os.path.exists(path):
            meta["status"] = "источник на паузе, взят прошлый файл"
            meta["stale"] = True
            return path, meta
        raise FeedError("Источник на паузе после сбоев, а прошлого файла фида нет.")

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if meta.get("source") == source and os.path.exists(path):
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    tmp = path + ".part"

    def download():
        if meter:
            meter.requests += 1
        req = urllib.request.Request(source, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
                return 200, resp.headers.get("ETag"), resp.headers.get("Last-Modified")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None, None
            if e.code == 429:
                if meter:
                    meter.http_429 += 1
                raise guard.Blocked(
                    "429", guard.parse_retry_after(e.headers.get("Retry-After")), "сервер фида просит ходить реже (429)"
                ) from None
            if e.code >= 500 or e.code == 408:
                raise _Transient(f"сервер фида ответил {e.code}") from None
            raise FeedError(_http_hint(e.code)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise _Transient(_short(e)) from None

    def on_retry(n, e, delay):
        if meter:
            meter.retries += 1
        log.warning("feed.retry", f"Фид не скачался ({e}), повтор {n} через {delay:.0f} с", attempt=n)

    try:
        code, etag, last_mod = guard.retry(
            download, attempts=attempts, base=retry_base, retry_on=(_Transient,), sleep=sleep, on_retry=on_retry
        )
    except _Transient as e:
        if meter:
            meter.errors += 1
        if os.path.exists(tmp):
            os.remove(tmp)
        if os.path.exists(path):
            meta["status"] = f"не скачался ({e}), взят прошлый файл"
            meta["stale"] = True
            log.warning("feed.stale", meta["status"])
            return path, meta
        raise FeedError(f"Фид не скачался: {e}. Проверьте ссылку и интернет.") from None
    if code == 304:
        meta.update(fetched_at=time.time(), status="не изменился", stale=False)
        _save_json(meta_path, meta)
        log.info("feed.not_modified", "Фид не изменился с прошлой загрузки")
        return path, meta
    bad = "Сервер вернул пустой файл" if os.path.getsize(tmp) < 64 else _not_a_feed(tmp)
    if bad:
        # прошлый хороший файл не затираем: с ним сбор пройдёт, а причина будет в предупреждении
        os.remove(tmp)
        if meter:
            meter.errors += 1
        hint = f"{bad}. Проверьте ссылку на фид в кабинете партнёрской сети"
        if meta.get("source") == source and os.path.exists(path):
            meta["status"] = f"{hint}; взят прошлый файл"
            meta["stale"] = True
            log.warning("feed.not_a_feed", meta["status"])
            return path, meta
        raise FeedError(hint + ".")
    os.replace(tmp, path)
    meta.update(
        source=source,
        etag=etag,
        last_modified=last_mod,
        fetched_at=time.time(),
        size=os.path.getsize(path),
        status="скачан",
        stale=False,
    )
    _save_json(meta_path, meta)
    log.info("feed.downloaded", "Фид скачан", bytes=meta["size"])
    return path, meta


class _Transient(Exception):
    """Сбой, который имеет смысл повторить: сеть, тайм-аут, 5xx."""


def _http_hint(code):
    if code in (401, 403):
        return (
            f"Сервер фида ответил {code}: доступ закрыт. Обычно это значит, что программа ещё не одобрила "
            "площадку или ссылка устарела — возьмите новую ссылку в кабинете."
        )
    if code == 404:
        return "Сервер фида ответил 404: такой ссылки нет. Скопируйте ссылку заново из кабинета партнёрской сети."
    if code == 429:
        return "Сервер фида просит ходить реже (429). Следующая загрузка — по расписанию."
    return f"Сервер фида ответил {code}."


def _not_a_feed(path):
    """Причина, если скачалась не выгрузка товаров (страница входа, ошибка в JSON), иначе None."""
    try:
        with _open(path) as f:
            head = f.read(4096)
    except (OSError, zipfile.BadZipFile, EOFError, FeedError):
        return "Скачанный файл фида не открывается (архив повреждён)"
    text = head.decode("utf-8", "ignore").lstrip("\ufeff \r\n\t").lower()
    if text.startswith(("<!doctype html", "<html")) or ("<html" in text[:1024] and "<yml_catalog" not in text):
        return "Вместо фида сервер вернул веб-страницу (вход в кабинет или ошибка)"
    if text.startswith(("{", "[")):
        return "Вместо фида сервер вернул сообщение об ошибке (JSON)"
    return None


def _short(e):
    s = str(getattr(e, "reason", e)) or e.__class__.__name__
    return s[:160]


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- открыть и узнать формат


def _open(path):
    """Двоичный поток с распакованным содержимым (gzip, zip или как есть)."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if magic == b"PK\x03\x04":
        z = zipfile.ZipFile(path)
        members = [i for i in z.infolist() if not i.is_dir()]
        if not members:
            raise FeedError("В архиве фида нет файлов.")
        biggest = max(members, key=lambda i: i.file_size)
        return z.open(biggest)
    return open(path, "rb")


def detect_format(path):
    with _open(path) as f:
        head = f.read(8192)
    text = head.decode("utf-8", "ignore").lower()
    if "<yml_catalog" in text or ("<offers" in text and "<offer" in text):
        return "yml"
    if "<rss" in text or "xmlns:g=" in text or "<feed" in text:
        return "google"
    if text.lstrip().startswith("<?xml") or text.lstrip().startswith("<"):
        return "yml"
    return "csv"


# ---------------------------------------------------------------- разбор


class Feed:
    """Результат чтения: категории (все), товары (только нужные) и сведения о файле."""

    def __init__(self, site, fmt):
        self.site = site
        self.format = fmt
        self.date = None  # дата выгрузки из самого файла (yml_catalog date), UTC
        self.shop = ""
        self.categories = {}  # id -> {"parent": id|None, "name": str, "count": int}
        self.offers = []  # отобранные товары
        self.total = 0  # всего позиций в файле
        self.foreign = 0  # позиций с адресом чужой сети
        self.no_price = 0
        self.tracker_links = 0  # ссылок, из которых не удалось достать адрес товара на сайте сети

    def chain(self, cat_id):
        """id категории и всех её родителей (от себя к корню)."""
        out, seen = [], set()
        while cat_id is not None and cat_id not in seen and cat_id in self.categories:
            seen.add(cat_id)
            out.append(cat_id)
            cat_id = self.categories[cat_id]["parent"]
        return out

    def path_name(self, cat_id):
        return " / ".join(self.categories[c]["name"] for c in reversed(self.chain(cat_id)))


def read(path, site, want=None, fmt=None):
    """Читает фид. want(offer, feed) -> bool решает, оставить ли товар (по умолчанию — все)."""
    fmt = fmt or detect_format(path)
    feed = Feed(site, fmt)
    reader = {"yml": _read_yml, "google": _read_google, "csv": _read_csv}[fmt]
    try:
        reader(path, feed, want)
    except ET.ParseError as e:
        raise FeedError(f"Файл фида повреждён или обрезан ({e}). Скачайте его заново.") from None
    if feed.total and feed.foreign > feed.total * 0.5:
        other = "Лемана ПРО" if site == "petrovich" else "Петровича"
        raise FeedError(f"Похоже, это фид другой сети: большинство ссылок ведут не туда (возможно, фид {other}).")
    return feed


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _accept(feed, offer, want):
    feed.total += 1
    if offer.pop("_tracker", False):
        feed.tracker_links += 1
    if offer["url"] and sites.site_of(offer["url"]) not in (None, feed.site):
        feed.foreign += 1
        return
    if offer["price"] is None:
        feed.no_price += 1
    cat = offer.get("category_id")
    if cat in feed.categories:
        feed.categories[cat]["count"] += 1
    if want is None or want(offer, feed):
        feed.offers.append(offer)


def _make_offer(
    site,
    offer_id,
    name,
    price,
    old_price,
    currency,
    available,
    url,
    category_id,
    vendor="",
    vendor_code="",
    params=None,
):
    real_url = sites.normalize_url(url) if url else ""
    tracker = bool(real_url) and not sites.site_of(real_url)
    if tracker:
        # ссылка-счётчик партнёрской сети, из которой не достать адрес товара: адресом товара её не считаем —
        # иначе сверка с сайтом открыла бы её в окне Edge (а это «переход по рекламе»)
        real_url = ""
    code = sites.code_from_url(real_url) if real_url else None
    return {
        "_tracker": tracker,
        "site": site,
        "key": sites.product_key(site, code=code, fallback_id=offer_id),
        "code": code or (str(offer_id).strip() if offer_id else ""),
        "offer_id": str(offer_id or "").strip(),
        "name": " ".join((name or "").split()),
        "price": price,
        "old_price": old_price if (old_price and price and old_price > price) else None,
        "currency": (currency or "RUB").upper().replace("RUR", "RUB"),
        "available": available,
        "url": real_url,
        "category_id": category_id,
        "vendor": (vendor or "").strip(),
        "vendor_code": (vendor_code or "").strip(),
        "params": params or {},
    }


def _read_yml(path, feed, want):
    root = offers_parent = None
    with _open(path) as f:
        for event, el in ET.iterparse(f, events=("start", "end")):
            tag = _local(el.tag)
            if event == "start":
                if root is None:
                    root = el
                    feed.date = parse_date(el.get("date"))
                elif tag == "offers":
                    offers_parent = el
                continue
            if tag == "category":
                cid = (el.get("id") or "").strip()
                if cid:
                    parent = (el.get("parentId") or el.get("parentid") or "").strip() or None
                    feed.categories[cid] = {"parent": parent, "name": " ".join((el.text or "").split()), "count": 0}
                el.clear()
            elif (
                tag == "name"
                and offers_parent is None
                and root is not None
                and _local(root.tag) in ("yml_catalog", "shop")
            ):
                if not feed.shop:
                    feed.shop = (el.text or "").strip()
            elif tag == "offer":
                _accept(feed, _yml_offer(el, feed.site), want)
                el.clear()
                if offers_parent is not None:
                    try:
                        offers_parent.remove(el)
                    except ValueError:
                        pass


def _yml_offer(el, site):
    get = {}
    params = {}
    for ch in el:
        t = _local(ch.tag)
        if t == "param":
            pname = " ".join((ch.get("name") or "").split())
            if pname:
                params[pname] = ((ch.text or "").strip(), (ch.get("unit") or "").strip())
        elif t not in get:
            get[t] = (ch.text or "").strip()
    name = get.get("name")
    if not name:
        name = " ".join(x for x in (get.get("typePrefix"), get.get("vendor"), get.get("model")) if x)
    return _make_offer(
        site,
        offer_id=el.get("id"),
        name=name,
        price=parse_number(get.get("price")),
        old_price=parse_number(get.get("oldprice") or get.get("old_price")),
        currency=get.get("currencyId") or get.get("currencyid"),
        available=parse_bool(el.get("available") if el.get("available") is not None else get.get("available")),
        url=get.get("url"),
        category_id=(get.get("categoryId") or get.get("categoryid") or "").strip() or None,
        vendor=get.get("vendor"),
        vendor_code=get.get("vendorCode") or get.get("vendorcode"),
        params=params,
    )


def _path_category(feed, path_text):
    """Категория из пути «Стройматериалы > Сухие смеси > Штукатурки»; id = сам путь."""
    parts = [p.strip() for p in re.split(r"\s*(?:>|/|\|)\s*", path_text or "") if p.strip()]
    parent = None
    cid = None
    for i in range(len(parts)):
        cid = " > ".join(parts[: i + 1])
        if cid not in feed.categories:
            feed.categories[cid] = {"parent": parent, "name": parts[i], "count": 0}
        parent = cid
    return cid


def _read_google(path, feed, want):
    with _open(path) as f:
        for _event, el in ET.iterparse(f, events=("end",)):
            tag = _local(el.tag)
            if tag not in ("item", "entry"):
                continue
            get: dict[str, str] = {}
            for ch in el:
                t = _local(ch.tag)
                if t == "link" and ch.get("href"):
                    get.setdefault("link", ch.get("href"))
                elif t not in get:
                    get[t] = (ch.text or "").strip()
            price, cur = _money(get.get("price"))
            sale, _ = _money(get.get("sale_price"))
            cat = _path_category(feed, get.get("product_type") or get.get("google_product_category") or "")
            offer = _make_offer(
                feed.site,
                offer_id=get.get("id"),
                name=get.get("title"),
                price=sale if sale else price,
                old_price=price if sale else None,
                currency=cur,
                available=parse_bool(get.get("availability")),
                url=get.get("link"),
                category_id=cat,
                vendor=get.get("brand"),
                vendor_code=get.get("mpn"),
            )
            _accept(feed, offer, want)
            el.clear()


_CSV_COLUMNS = {
    "offer_id": ("id", "offer_id", "offerid", "offer id", "sku", "product_id", "артикул", "код", "код товара"),
    "name": ("name", "title", "наименование", "название", "товар", "model"),
    "price": ("price", "цена", "цена, руб", "price_rub"),
    "old_price": ("oldprice", "old_price", "price_old", "старая цена", "цена старая", "цена без скидки"),
    "url": ("url", "link", "ссылка", "product_url", "deeplink"),
    "available": ("available", "availability", "наличие", "in_stock", "instock"),
    "category": ("categoryid", "category_id", "category", "категория", "product_type", "раздел", "categoryname"),
    "vendor": ("vendor", "brand", "бренд", "производитель", "марка"),
    "vendor_code": ("vendorcode", "vendor_code", "mpn", "артикул производителя"),
    "currency": ("currencyid", "currency", "валюта"),
}


CSV_PROBE_BYTES = 1 << 20  # по первому мегабайту решаем, UTF-8 это или cp1251


def _csv_encoding(path):
    """utf-8-sig или cp1251 — по началу файла (обрезанный на границе последний символ не считается ошибкой)."""
    with _open(path) as raw:
        head = raw.read(CSV_PROBE_BYTES)
    try:
        codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=False)
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "cp1251"


def _read_csv(path, feed, want):
    """CSV читается потоком, как и XML: каталог сети в сотни мегабайт целиком в память не грузится."""
    enc = _csv_encoding(path)
    with _open(path) as raw, io.TextIOWrapper(raw, encoding=enc, errors="replace", newline="") as text:
        _read_csv_rows(text, feed, want)


def _read_csv_rows(text, feed, want):
    sample = text.read(20000)
    sample += text.readline()  # до конца строки: Sniffer не должен видеть обрезанную запись
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = None
    lines = itertools.chain(io.StringIO(sample), text)
    if dialect is not None:
        rows = csv.reader(lines, dialect)
    else:
        rows = csv.reader(lines, delimiter=";" if sample.count(";") > sample.count(",") else ",")
    header = next(rows, None)
    if not header:
        raise FeedError("CSV-файл фида пустой.")
    norm = [h.strip().lower().replace("ё", "е") for h in header]
    col = {}
    for field, names in _CSV_COLUMNS.items():
        for i, h in enumerate(norm):
            if h in names and field not in col:
                col[field] = i
    if "price" not in col or ("name" not in col and "url" not in col):
        raise FeedError("В CSV не нашлись колонки цены и названия/ссылки. Проверьте, что это товарный фид.")
    known = set(col.values())
    param_cols = [
        (i, re.sub(r"^param[_:\s]*", "", header[i].strip(), flags=re.I))
        for i in range(len(header))
        if i not in known
        and header[i].strip()
        and not norm[i].startswith(("picture", "image", "description", "описание", "изображ"))
    ]

    def g(row, field):
        i = col.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue
        cat_raw = g(row, "category")
        if cat_raw and not re.fullmatch(r"\d+", cat_raw):
            cat = _path_category(feed, cat_raw)
        else:
            cat = cat_raw or None
            if cat and cat not in feed.categories:
                feed.categories[cat] = {"parent": None, "name": f"Категория {cat}", "count": 0}
        params = {}
        for i, pname in param_cols:
            if i < len(row) and row[i].strip():
                params[pname] = (row[i].strip(), "")
        price, cur = _money(g(row, "price"))
        old, _ = _money(g(row, "old_price"))
        offer = _make_offer(
            feed.site,
            offer_id=g(row, "offer_id"),
            name=g(row, "name"),
            price=price,
            old_price=old,
            currency=g(row, "currency") or cur,
            available=parse_bool(g(row, "available")),
            url=g(row, "url"),
            category_id=cat,
            vendor=g(row, "vendor"),
            vendor_code=g(row, "vendor_code"),
            params=params,
        )
        _accept(feed, offer, want)


# ---------------------------------------------------------------- значения


def parse_number(s):
    """Цена из текста: «1 234,50», «1,234.50», «1.234,50», «690.00 RUB» → float; ноль и мусор → None."""
    if s is None:
        return None
    s = re.sub(r"[\s\u00a0\u202f']", "", str(s))  # пробелы, неразрывные пробелы и апостроф — разделители тысяч
    m = re.search(r"-?\d[\d.,]*", s)
    if not m:
        return None
    num = m.group(0).rstrip(".,")
    dot, comma = num.rfind("."), num.rfind(",")
    if dot >= 0 and comma >= 0:  # оба разделителя: десятичный — тот, что правее
        num = num.replace(",", "") if dot > comma else num.replace(".", "").replace(",", ".")
    elif comma >= 0:  # «1234,50» — запятая десятичная; «1,234,567» — тысячи
        num = num.replace(",", ".") if num.count(",") == 1 else num.replace(",", "")
    elif num.count(".") > 1:  # «1.234.567» — точки разделяют тысячи
        num = num.replace(".", "")
    try:
        v = float(num)
    except ValueError:
        return None
    return v if v > 0 else None


def _money(s):
    if not s:
        return None, None
    cur = None
    m = re.search(r"\b([A-Z]{3})\b", s)
    if m:
        cur = m.group(1)
    elif "₽" in s or "руб" in s.lower():
        cur = "RUB"
    return parse_number(s), cur


def parse_bool(s):
    if s is None:
        return None
    t = str(s).strip().lower()
    if t in ("true", "1", "yes", "да", "in stock", "in_stock", "instock", "в наличии", "есть", "available"):
        return True
    if t in ("false", "0", "no", "нет", "out of stock", "out_of_stock", "outofstock", "нет в наличии", "preorder"):
        return False
    return None


MSK = timezone(timedelta(hours=3))


def parse_date(s):
    """Дата выгрузки из файла → секунды UTC. Московское время, если пояс не указан."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=MSK).timestamp()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # без пояса — московское время, а не пояс компьютера («2026-10-06», «2026-10-06T08:00:00.123»)
        return (dt if dt.tzinfo else dt.replace(tzinfo=MSK)).timestamp()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        return None


def file_digest(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
