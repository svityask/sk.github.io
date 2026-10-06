"""База SQLite: что отслеживаем, товары, история цен (только изменения), сборы, решения по «На проверку»."""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import sqlite3
import time
from collections.abc import Iterator

from . import kinds, units

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tracked(
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('category','section','product')),
    ref TEXT NOT NULL,              -- id категории фида или адрес раздела/товара
    title TEXT NOT NULL DEFAULT '',
    added REAL NOT NULL,
    UNIQUE(site, kind, ref)
);
CREATE TABLE IF NOT EXISTS categories(
    site TEXT NOT NULL, id TEXT NOT NULL, parent TEXT, name TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(site, id)
);
CREATE TABLE IF NOT EXISTS products(
    key TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    code TEXT,
    name TEXT,
    url TEXT,
    vendor TEXT,
    category_id TEXT,
    group_id INTEGER,               -- запись «Что отслеживаем», через которую товар попал в сбор
    pack_qty REAL,
    pack_unit TEXT,
    is_ours INTEGER NOT NULL DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    last_run INTEGER,
    price REAL, old_price REAL, available INTEGER, source TEXT, city TEXT,
    prev_price REAL, prev_source TEXT
);
CREATE INDEX IF NOT EXISTS products_run ON products(last_run);
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    price REAL, old_price REAL, available INTEGER,
    source TEXT NOT NULL,           -- feed / edge
    city TEXT
);
CREATE INDEX IF NOT EXISTS history_key ON history(key, ts);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY,
    started REAL NOT NULL,
    finished REAL,
    status TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS kind_overrides(
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS market_points(   -- Основит против аналогов в каждом сборе: для графиков
    run_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    price REAL, per_unit REAL, median REAL, min REAL, count INTEGER, level TEXT, kind TEXT, unit TEXT,
    PRIMARY KEY(run_id, key)
);
CREATE INDEX IF NOT EXISTS market_points_key ON market_points(key, ts);
CREATE TABLE IF NOT EXISTS site_reads(      -- удачные полные чтения фида: от них считаем «появился/пропал»
    run_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    source TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY(run_id, site, source)
);
CREATE TABLE IF NOT EXISTS crosscheck(      -- сверка фида с полкой
    run_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    key TEXT NOT NULL,
    feed_price REAL NOT NULL,
    site_price REAL NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY(run_id, key)
);
CREATE TABLE IF NOT EXISTS decisions(
    key TEXT NOT NULL,
    kind TEXT NOT NULL,             -- exclude — не конкурент; accept — проверено, всё верно
    note TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY(key, kind)
);
"""


# Миграции: PRAGMA user_version — номер последней применённой. Каждая выполняется в одной транзакции.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA.replace("PRAGMA journal_mode=WAL;", "")),
    (
        2,
        """
ALTER TABLE history ADD COLUMN run_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS history_run ON history(key, run_id) WHERE run_id IS NOT NULL;
""",
    ),
    (
        3,
        """
CREATE TABLE IF NOT EXISTS metrics(         -- метрики сбора по источнику: сеть × фид/сайт
    run_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    source TEXT NOT NULL,                   -- feed / edge
    started REAL NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,    -- скачиваний фида / открытых страниц
    pages INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    http_429 INTEGER NOT NULL DEFAULT 0,
    captcha INTEGER NOT NULL DEFAULT 0,
    empty INTEGER NOT NULL DEFAULT 0,       -- страниц/карточек без цены, пустых фидов
    prices INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'ok',     -- ok / partial / blocked / failed / skipped
    PRIMARY KEY(run_id, site, source)
);
CREATE TABLE IF NOT EXISTS breakers(        -- предохранитель источника
    site TEXT NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'closed',   -- closed / open / half
    failures INTEGER NOT NULL DEFAULT 0,    -- подряд
    opens INTEGER NOT NULL DEFAULT 0,       -- сколько раз подряд открывался (для удвоения паузы)
    opened_at REAL,
    until REAL,
    reason TEXT,
    PRIMARY KEY(site, source)
);
CREATE TABLE IF NOT EXISTS locks(           -- один сбор за раз: окно и расписание не пишут одновременно
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    pid INTEGER,
    started REAL NOT NULL,
    heartbeat REAL NOT NULL
);
""",
    ),
    (
        4,
        """
CREATE TABLE IF NOT EXISTS pack_overrides(  -- фасовка, заданная вручную (сильнее названия и фида)
    key TEXT PRIMARY KEY,
    qty REAL NOT NULL,                      -- в базовой единице: кг или л
    unit TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spot_checks(     -- выборочная проверка: человек сверил карточку с сайтом
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    verdict TEXT NOT NULL,                  -- ok / price / pack / kind / not_competitor
    note TEXT,                              -- что было не так: цена на сайте, правильная фасовка, вид
    price REAL, pack TEXT, kind TEXT        -- что показывало приложение в момент проверки
);
CREATE INDEX IF NOT EXISTS spot_checks_key ON spot_checks(key, ts);
CREATE TABLE IF NOT EXISTS kv(              -- мелкие значения: текущая выборка и т. п.
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""",
    ),
    (
        5,
        """
ALTER TABLE products ADD COLUMN found_by TEXT NOT NULL DEFAULT 'tracked';  -- tracked / auto (автопоиск аналогов)
""",
    ),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(path: str) -> sqlite3.Connection:
    """Соединение в режиме autocommit: атомарность — только явными транзакциями (transaction())."""
    con = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> int:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in MIGRATIONS:
        if number <= version:
            continue
        with transaction(con):
            for stmt in _split(sql):
                try:
                    con.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):  # колонку уже добавили руками — не беда
                        raise
            con.execute(f"PRAGMA user_version={number}")
        version = number
    return version


def _split(sql: str) -> list[str]:
    """SQL-скрипт → отдельные команды (в схеме нет «;» внутри строк)."""
    code = "\n".join(ln.split("--", 1)[0] for ln in sql.splitlines())  # сначала комментарии: в них бывает «;»
    return [stmt.strip() for stmt in code.split(";") if stmt.strip()]


_sp_counter = itertools.count(1)


@contextlib.contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Всё или ничего. Вложенные блоки — точки сохранения (SAVEPOINT)."""
    if con.in_transaction:
        name = f"sp{next(_sp_counter)}"
        con.execute(f"SAVEPOINT {name}")
        try:
            yield con
        except BaseException:
            con.execute(f"ROLLBACK TO {name}")
            con.execute(f"RELEASE {name}")
            raise
        con.execute(f"RELEASE {name}")
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


# ---------------------------------------------------------------- блокировка «один сбор за раз»


def acquire_lock(con: sqlite3.Connection, name: str, owner: str, ttl: float = 900) -> dict | None:
    """True-подобный None, если взяли; иначе сведения о том, кто держит."""
    now = time.time()
    with transaction(con):
        row = con.execute("SELECT * FROM locks WHERE name=?", (name,)).fetchone()
        if row and row["owner"] != owner and row["heartbeat"] > now - ttl:
            return dict(row)
        con.execute(
            "INSERT OR REPLACE INTO locks(name,owner,pid,started,heartbeat) VALUES(?,?,?,?,?)",
            (name, owner, os.getpid(), now, now),
        )
    return None


def heartbeat(con: sqlite3.Connection, name: str, owner: str) -> None:
    con.execute("UPDATE locks SET heartbeat=? WHERE name=? AND owner=?", (time.time(), name, owner))


def release_lock(con: sqlite3.Connection, name: str, owner: str) -> None:
    con.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))


def lock_info(con: sqlite3.Connection, name: str, ttl: float = 900) -> dict | None:
    row = con.execute("SELECT * FROM locks WHERE name=?", (name,)).fetchone()
    if row and row["heartbeat"] > time.time() - ttl:
        return dict(row)
    return None


def recover_interrupted(con: sqlite3.Connection, keep_id: int | None = None) -> int:
    """Сборы, оставшиеся «идёт» после выключения компьютера или сбоя, помечаются «прерван»."""
    cur = con.execute(
        "UPDATE runs SET status='прерван', finished=COALESCE(finished, started) WHERE status='идёт' AND id IS NOT ?",
        (keep_id,),
    )
    return cur.rowcount


# ---------------------------------------------------------------- метрики

METRIC_FIELDS = ("seconds", "requests", "pages", "errors", "retries", "http_429", "captcha", "empty", "prices")


def save_metrics(con: sqlite3.Connection, run_id: int, rows: list[dict]) -> None:
    with transaction(con):
        for m in rows:
            con.execute(
                "INSERT OR REPLACE INTO metrics(run_id,site,source,started,seconds,requests,pages,errors,retries,"
                "http_429,captcha,empty,prices,outcome) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    m["site"],
                    m["source"],
                    m["started"],
                    *[m.get(f, 0) for f in METRIC_FIELDS],
                    m.get("outcome", "ok"),
                ),
            )


def metrics_recent(con: sqlite3.Connection, runs: int = 20) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            "SELECT * FROM metrics WHERE run_id IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?) "
            "ORDER BY run_id DESC, site, source",
            (runs,),
        )
    ]


# ---------------------------------------------------------------- что отслеживаем


def tracked(con, site=None):
    q = "SELECT * FROM tracked" + (" WHERE site=?" if site else "") + " ORDER BY site, kind, title"
    return [dict(r) for r in con.execute(q, (site,) if site else ())]


def add_tracked(con, site, kind, ref, title=""):
    con.execute(
        "INSERT OR IGNORE INTO tracked(site,kind,ref,title,added) VALUES(?,?,?,?,?)",
        (site, kind, ref, title, time.time()),
    )


def remove_tracked(con, tid):
    con.execute("DELETE FROM tracked WHERE id=?", (tid,))


# ---------------------------------------------------------------- категории фида


def save_categories(con, site, cats):
    with transaction(con):
        con.execute("DELETE FROM categories WHERE site=?", (site,))
        con.executemany(
            "INSERT INTO categories(site,id,parent,name,count) VALUES(?,?,?,?,?)",
            [(site, cid, c["parent"], c["name"], c["count"]) for cid, c in cats.items()],
        )


def categories(con, site):
    return {
        r["id"]: {"parent": r["parent"], "name": r["name"], "count": r["count"]}
        for r in con.execute("SELECT * FROM categories WHERE site=?", (site,))
    }


# ---------------------------------------------------------------- сборы и цены


def start_run(con):
    cur = con.execute("INSERT INTO runs(started,status) VALUES(?,?)", (time.time(), "идёт"))
    return cur.lastrowid


def finish_run(con, run_id, status, summary):
    con.execute(
        "UPDATE runs SET finished=?, status=?, summary=? WHERE id=?",
        (time.time(), status, json.dumps(summary, ensure_ascii=False), run_id),
    )


def last_runs(con, n=10):
    out = []
    for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)):
        d = dict(r)
        d["summary"] = json.loads(d["summary"]) if d["summary"] else {}
        out.append(d)
    return out


def record(con, run_id, item, ts=None):
    """Пишет замер. История — только если цена, старая цена или наличие изменились.

    item: key, site, code, name, url, vendor, category_id, group_id, pack_qty, pack_unit, is_ours,
          price, old_price, available, source, city
    """
    ts = ts or time.time()
    ov = con.execute("SELECT qty, unit FROM pack_overrides WHERE key=?", (item["key"],)).fetchone()
    if ov:  # ручная фасовка сильнее названия и фида
        item = {**item, "pack_qty": ov["qty"], "pack_unit": ov["unit"]}
    row = con.execute("SELECT * FROM products WHERE key=?", (item["key"],)).fetchone()
    avail = None if item.get("available") is None else int(bool(item["available"]))
    changed = (
        row is None
        or row["price"] != item["price"]
        or row["old_price"] != item.get("old_price")
        or row["available"] != avail
    )
    if row is None:
        con.execute(
            """INSERT INTO products(key,site,code,name,url,vendor,category_id,group_id,pack_qty,pack_unit,is_ours,
               first_seen,last_seen,last_run,price,old_price,available,source,city,found_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item["key"],
                item["site"],
                item.get("code"),
                item.get("name"),
                item.get("url"),
                item.get("vendor"),
                item.get("category_id"),
                item.get("group_id"),
                item.get("pack_qty"),
                item.get("pack_unit"),
                int(bool(item.get("is_ours"))),
                ts,
                ts,
                run_id,
                item["price"],
                item.get("old_price"),
                avail,
                item["source"],
                item.get("city"),
                item.get("found_by") or "tracked",
            ),
        )
    else:
        prev_price = row["price"] if changed else row["prev_price"]
        prev_source = row["source"] if changed else row["prev_source"]
        con.execute(
            """UPDATE products SET code=?, name=?, url=?, vendor=?, category_id=COALESCE(?,category_id),
               group_id=COALESCE(?,group_id), pack_qty=?, pack_unit=?, is_ours=?, last_seen=?, last_run=?,
               price=?, old_price=?, available=?, source=?, city=?, prev_price=?, prev_source=?, found_by=?
               WHERE key=?""",
            (
                item.get("code") or row["code"],
                item.get("name") or row["name"],
                item.get("url") or row["url"],
                item.get("vendor") or row["vendor"],
                item.get("category_id"),
                item.get("group_id"),
                item.get("pack_qty"),
                item.get("pack_unit"),
                int(bool(item.get("is_ours"))),
                ts,
                run_id,
                item["price"],
                item.get("old_price"),
                avail,
                item["source"],
                item.get("city"),
                prev_price,
                prev_source,
                item.get("found_by") or "tracked",
                item["key"],
            ),
        )
    if changed:
        # run_id + уникальный индекс: повтор записи того же сбора не задваивает историю
        con.execute(
            "INSERT OR IGNORE INTO history(key,ts,price,old_price,available,source,city,run_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (item["key"], ts, item["price"], item.get("old_price"), avail, item["source"], item.get("city"), run_id),
        )
    return {
        "new": row is None,
        "price_changed": row is not None and row["price"] != item["price"],
        "avail_from": None if row is None else row["available"],
        "avail_to": avail,
    }


def products_of_run(con, run_id):
    return [dict(r) for r in con.execute("SELECT * FROM products WHERE last_run=? ORDER BY site, name", (run_id,))]


def history_since(con, since_ts):
    return [
        dict(r)
        for r in con.execute(
            """SELECT h.*, p.name, p.site, p.url, p.is_ours FROM history h JOIN products p ON p.key=h.key
           WHERE h.ts>=? ORDER BY h.ts DESC LIMIT 20000""",
            (since_ts,),
        )
    ]


def last_price_of(con, key):
    r = con.execute("SELECT price, source, ts FROM history WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- решения по «На проверку»


def decisions(con) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in con.execute("SELECT key, kind FROM decisions"):
        out.setdefault(r["key"], set()).add(r["kind"])
    return out


def decide(con, key, kind, note=""):
    if kind == "undo":
        con.execute("DELETE FROM decisions WHERE key=?", (key,))
    else:
        con.execute(
            "INSERT OR REPLACE INTO decisions(key,kind,note,ts) VALUES(?,?,?,?)", (key, kind, note, time.time())
        )


# ---------------------------------------------------------------- вид товара (ручная правка)


def kind_overrides(con):
    return {r["key"]: r["kind"] for r in con.execute("SELECT key, kind FROM kind_overrides")}


def set_kind(con, key, kind):
    kind = " ".join((kind or "").split())
    if kind:
        con.execute("INSERT OR REPLACE INTO kind_overrides(key,kind,ts) VALUES(?,?,?)", (key, kind, time.time()))
    else:
        con.execute("DELETE FROM kind_overrides WHERE key=?", (key,))


# ---------------------------------------------------------------- фасовка (ручная правка)


def pack_overrides(con):
    return {r["key"]: (r["qty"], r["unit"]) for r in con.execute("SELECT key, qty, unit FROM pack_overrides")}


def set_pack(con, key, text):
    """Ручная фасовка из текста («25 кг», «0,9 л»). Пустой текст — вернуть автоматическую.

    Пишется сразу и в products, чтобы карточка и отчёт показали новую цену за единицу без нового сбора.
    Возвращает (qty, unit) или (None, None); ValueError — если текст не похож на фасовку.
    """
    text = " ".join((text or "").split())
    row = con.execute("SELECT name FROM products WHERE key=?", (key,)).fetchone()
    with transaction(con):
        if not text:
            con.execute("DELETE FROM pack_overrides WHERE key=?", (key,))
            qty, unit = units.pack_of(row["name"] if row else "", None, kinds.pack_unit(row["name"] if row else ""))
        else:
            qty, unit = units.from_text(text)
            if not qty:
                raise ValueError("не понял фасовку — напишите, например, «25 кг» или «0,9 л»")
            con.execute(
                "INSERT OR REPLACE INTO pack_overrides(key,qty,unit,ts) VALUES(?,?,?,?)", (key, qty, unit, time.time())
            )
        if row:
            con.execute("UPDATE products SET pack_qty=?, pack_unit=? WHERE key=?", (qty, unit, key))
    return qty, unit


# ---------------------------------------------------------------- мелкие значения


def kv_get(con, name, default=None):
    r = con.execute("SELECT value FROM kv WHERE name=?", (name,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def kv_set(con, name, value):
    con.execute("INSERT OR REPLACE INTO kv(name,value) VALUES(?,?)", (name, json.dumps(value, ensure_ascii=False)))


# ---------------------------------------------------------------- выборочная проверка


def spot_add(con, key, verdict, note="", shown=None):
    shown = shown or {}
    con.execute(
        "INSERT INTO spot_checks(key,ts,verdict,note,price,pack,kind) VALUES(?,?,?,?,?,?,?)",
        (key, time.time(), verdict, note, shown.get("price"), shown.get("pack"), shown.get("kind")),
    )


def spot_undo(con, key, since):
    con.execute("DELETE FROM spot_checks WHERE key=? AND ts>=?", (key, since))


def spot_latest(con, since):
    """Последняя проверка каждого товара начиная с since: {key: строка}."""
    out = {}
    for r in con.execute("SELECT * FROM spot_checks WHERE ts>=? ORDER BY ts", (since,)):
        out[r["key"]] = dict(r)
    return out


# ---------------------------------------------------------------- точки рынка, чтения, сверка


def save_market_points(con, run_id, rows, ts=None):
    ts = ts or time.time()
    with transaction(con):
        con.executemany(
            "INSERT OR REPLACE INTO market_points(run_id,ts,key,price,per_unit,median,min,count,level,kind,unit) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    ts,
                    r["key"],
                    r.get("price"),
                    r.get("per_unit"),
                    r.get("median"),
                    r.get("min"),
                    r.get("count"),
                    r.get("level"),
                    r.get("kind"),
                    r.get("unit"),
                )
                for r in rows
            ],
        )


def market_points(con, key, since_ts=0):
    return [
        dict(r) for r in con.execute("SELECT * FROM market_points WHERE key=? AND ts>=? ORDER BY ts", (key, since_ts))
    ]


def market_points_since(con, since_ts):
    return [
        dict(r)
        for r in con.execute(
            "SELECT m.*, p.name, p.site, p.url FROM market_points m JOIN products p ON p.key=m.key "
            "WHERE m.ts>=? ORDER BY m.ts",
            (since_ts,),
        )
    ]


def mark_read(con, run_id, site, source="feed"):
    con.execute(
        "INSERT OR REPLACE INTO site_reads(run_id,site,source,ts) VALUES(?,?,?,?)", (run_id, site, source, time.time())
    )


def previous_read(con, site, run_id, source="feed"):
    r = con.execute(
        "SELECT run_id, ts FROM site_reads WHERE site=? AND source=? AND run_id<? ORDER BY run_id DESC LIMIT 1",
        (site, source, run_id),
    ).fetchone()
    return dict(r) if r else None


def save_crosscheck(con, run_id, site, rows):
    ts = time.time()
    with transaction(con):
        con.executemany(
            "INSERT OR REPLACE INTO crosscheck(run_id,site,key,feed_price,site_price,ts) VALUES(?,?,?,?,?,?)",
            [(run_id, site, k, f, s, ts) for k, f, s in rows],
        )


def history_of(con, key, since_ts=0):
    return [
        dict(r)
        for r in con.execute(
            "SELECT ts, price, old_price, available, source, city FROM history WHERE key=? AND ts>=? ORDER BY ts",
            (key, since_ts),
        )
    ]


def decisions_list(con):
    return [
        dict(r)
        for r in con.execute(
            "SELECT d.key, d.kind, d.ts, p.name, p.url, p.site FROM decisions d LEFT JOIN products p ON p.key=d.key "
            "ORDER BY d.ts DESC"
        )
    ]
