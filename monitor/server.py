"""Локальный сервер окна приложения (только 127.0.0.1, с ключом сессии)."""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import (
    backup,
    cdp,
    collect,
    config,
    db,
    dumps,
    extract,
    feeds,
    guard,
    health,
    kinds,
    log,
    schedule,
    selftest,
    sites,
    spot,
    units,
)
from .visitor import Visitor

UI_DIR = os.path.join(config.ROOT, "ui")


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.settings = config.load()
        self._local = threading.local()  # своё соединение с базой на каждый поток сервера
        self.lock = threading.Lock()
        self.status = collect.Status()
        self.last_ping = time.time()
        self._health: tuple[float, dict[str, Any] | None] = (0.0, None)
        self._catalog_cache: tuple[tuple, list[tuple[str, dict[str, Any]]]] | None = None
        if not db.lock_info(self.con, collect.LOCK):
            db.recover_interrupted(self.con)
        log.info("app.start", f"Окно приложения открыто, версия {config.VERSION}")
        threading.Thread(target=schedule.upgrade, daemon=True).start()

    @property
    def con(self):
        c = getattr(self._local, "con", None)
        if c is None:
            c = self._local.con = db.connect(config.DB_PATH)
        return c

    # ------------------------------------------------------------ сбор

    def start_run(self, force_feed=False):
        with self.lock:
            if self.status.running:
                return {"started": False, "reason": "сбор уже идёт"}
            holder = db.lock_info(self.con, collect.LOCK)
            if holder:
                return {"started": False, "reason": "сейчас идёт сбор по расписанию — дождитесь его окончания"}
            self.status = collect.Status()
            self.status.running = True
        settings = config.load()

        def work():
            collect.run(settings, self.status, interactive=True, force_feed=force_feed)
            self._health = (0.0, None)

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def health_status(self):
        """Краткий итог здоровья для Главной; полная проверка — не чаще раза в минуту."""
        ts, h = self._health
        if h is None or time.time() - ts > 60:
            h = health.check(self.con, config.load())
            self._health = (time.time(), h)
        return {"status": h["status"], "problems": [c for c in h["checks"] if c["status"] != "ok"]}

    def condition(self):
        """Вкладка «Состояние»."""
        h = health.check(self.con, config.load())
        self._health = (time.time(), h)
        runs = {r["id"]: r for r in db.last_runs(self.con, 15)}
        metrics = db.metrics_recent(self.con, 15)
        for m in metrics:
            r = runs.get(m["run_id"])
            m["run_status"] = r["status"] if r else ""
            m["site_title"] = sites.SITES.get(m["site"], {}).get("title", m["site"])
        return {
            "health": h,
            "metrics": metrics,
            "breakers": [
                {**b, "site_title": sites.SITES.get(b["site"], {}).get("title", b["site"])}
                for b in guard.states(self.con)
            ],
            "backups": backup.items()[:12],
            "failures": dumps.recent(15),
            "running": db.lock_info(self.con, collect.LOCK),
            "runs": [{k: r[k] for k in ("id", "started", "finished", "status")} for r in runs.values()],
        }

    # ------------------------------------------------------------ состояние

    def state(self):
        runs = db.last_runs(self.con, 8)
        tracked = db.tracked(self.con)
        counts = {}
        for row in self.con.execute("SELECT group_id, COUNT(*) n, SUM(is_ours) o FROM products GROUP BY group_id"):
            counts[row["group_id"]] = (row["n"], row["o"] or 0)
        for t in tracked:
            t["products"], t["ours"] = counts.get(t["id"], (0, 0))
        decisions = db.decisions(self.con)
        last = next((r for r in runs if r["status"] != "идёт"), None)
        if last:
            items = last["summary"].get("review_items") or []
            last["summary"]["review_items"] = [
                i
                for i in items
                if "exclude" not in decisions.get(i["key"], set())
                and f"accept:{i['signature']}" not in decisions.get(i["key"], set())
            ]
        feeds_meta = {}
        for site in sites.SITES:
            p = os.path.join(config.FEEDS_DIR, f"{site}.feed.json")
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        feeds_meta[site] = json.load(f)
                except (OSError, ValueError):
                    pass
        return {
            "version": config.VERSION,
            "settings": config.load(),
            "tracked": tracked,
            "runs": runs,
            "last": last,
            "status": self.status.snapshot(),
            "feeds": feeds_meta,
            "site_titles": {k: v["title"] for k, v in sites.SITES.items()},
            "categories_loaded": {
                s: bool(self.con.execute("SELECT 1 FROM categories WHERE site=? LIMIT 1", (s,)).fetchone())
                for s in sites.SITES
            },
            "schedule": schedule.state(),
            "health": self.health_status(),
            "has_success": bool(self.con.execute("SELECT 1 FROM runs WHERE status='готово' LIMIT 1").fetchone()),
            "spot": spot.stats(self.con),
        }

    # ------------------------------------------------------------ что отслеживаем

    def add_links(self, site_hint, text):
        added, errors = [], []
        for line in (text or "").replace(",", "\n").split():
            line = line.strip()
            if not line.startswith(("http", "petrovich", "lemanapro", "leroymerlin", "moscow.")):
                continue
            site = sites.site_of(line)
            if not site:
                errors.append(f"{line} — это не Петрович и не Лемана ПРО")
                continue
            url = sites.normalize_url(line)
            if sites.is_product_url(url):
                kind = "product"
            elif sites.is_section_url(url):
                kind = "section"
            else:
                errors.append(f"{line} — не похоже ни на раздел каталога, ни на карточку товара")
                continue
            title = ""
            if kind == "section":
                slug = [p for p in urlsplit(url).path.split("/") if p][-1]
                title = slug.replace("-", " ").capitalize() if not slug.isdigit() else f"Раздел {slug}"
            db.add_tracked(self.con, site, kind, url, title)
            added.append(url)
        return {"added": added, "errors": errors}

    def find_categories(self, site, q, limit=60):
        cats = db.categories(self.con, site)
        if not cats:
            return []
        total = {cid: c["count"] for cid, c in cats.items()}
        for c in cats.values():  # суммируем товары вверх по дереву
            seen, p = set(), c["parent"]
            while p and p in cats and p not in seen:
                seen.add(p)
                total[p] += c["count"]
                p = cats[p]["parent"]

        def path(cid):
            names, seen = [], set()
            while cid and cid in cats and cid not in seen:
                seen.add(cid)
                names.append(cats[cid]["name"])
                cid = cats[cid]["parent"]
            return " / ".join(reversed(names))

        words = [w for w in (q or "").lower().replace("ё", "е").split() if w]
        out = []
        for cid, c in cats.items():
            p = path(cid)
            if words and not all(w in p.lower().replace("ё", "е") for w in words):
                continue
            if total[cid] == 0:
                continue
            out.append({"id": cid, "name": c["name"], "path": p, "count": total[cid]})
        out.sort(key=lambda x: (-x["count"] if not words else len(x["path"]), x["path"]))
        return out[:limit]

    # ------------------------------------------------------------ товары

    def _last_run_id(self):
        r = self.con.execute("SELECT id FROM runs WHERE status!='идёт' ORDER BY id DESC LIMIT 1").fetchone()
        return r["id"] if r else None

    def _enrich(self, p, overrides, decisions, brand):
        from .analysis import is_ours

        p["is_ours"], _ = is_ours(p.get("name"), p.get("vendor"), brand)
        p["per_unit"] = units.per_unit(p.get("price"), p.get("pack_qty"))
        p["pack"] = units.pack_label(p.get("pack_qty"), p.get("pack_unit"))
        a, manual = kinds.attrs_of(p, overrides)
        p["attrs"], p["kind"], p["kind_manual"] = a, kinds.label(a) or "", manual
        d = decisions.get(p["key"], set())
        p["excluded"] = "exclude" in d
        p["site_title"] = sites.SITES.get(p["site"], {}).get("title", p["site"])
        return p

    CATALOG_DAYS = 60  # вкладка «Товары» показывает товары, которые видели за последние 60 дней

    def _catalog_signature(self, brand):
        """Отпечаток данных вкладки «Товары»: меняется после сбора и после любой ручной правки."""
        row = self.con.execute(
            "SELECT (SELECT COUNT(*) || ':' || IFNULL(MAX(last_seen), 0) || ':' || IFNULL(TOTAL(price), 0) "
            "        || ':' || IFNULL(TOTAL(pack_qty), 0) FROM products),"
            " (SELECT COUNT(*) || ':' || IFNULL(MAX(ts), 0) FROM kind_overrides),"
            " (SELECT COUNT(*) || ':' || IFNULL(MAX(ts), 0) FROM pack_overrides),"
            " (SELECT COUNT(*) || ':' || IFNULL(MAX(ts), 0) FROM decisions),"
            " (SELECT IFNULL(MAX(id), 0) FROM runs WHERE status!='идёт')"
        ).fetchone()
        return (*tuple(row), tuple(brand), int(time.time() // 3600))  # и раз в час — граница «60 дней» сдвигается

    def _catalog(self):
        """Все товары вкладки «Товары», уже разобранные (вид, фасовка, «наш»), со строкой для поиска.

        Разбор нескольких десятков тысяч названий занимает сотни миллисекунд, а поиск идёт на каждое нажатие
        клавиши. Поэтому разобранный каталог хранится, пока не изменились данные (см. _catalog_signature).
        Словари в кэше общие для всех запросов — после сборки их не меняем.
        """
        brand = config.load().get("brand_words") or []
        sig = self._catalog_signature(brand)
        cached = self._catalog_cache
        if cached and cached[0] == sig:
            return cached[1]
        last = self._last_run_id()
        overrides, decisions = db.kind_overrides(self.con), db.decisions(self.con)
        items = []
        for r in self.con.execute(
            "SELECT * FROM products WHERE last_seen>=? ORDER BY name", (time.time() - self.CATALOG_DAYS * 86400,)
        ):
            p = self._enrich(dict(r), overrides, decisions, brand)
            p["in_last_run"] = p.get("last_run") == last
            p.pop("attrs", None)
            hay = " ".join([p.get("name") or "", p["kind"], p.get("vendor") or "", p.get("code") or ""])
            items.append((hay.lower().replace("ё", "е"), p))
        items.sort(
            key=lambda x: (
                not x[1]["is_ours"],
                not x[1]["in_last_run"],
                x[1]["site"],
                x[1]["kind"] or "я",
                x[1].get("per_unit") or 0,
            )
        )
        self._catalog_cache = (sig, items)
        return items

    def products(self, q="", site="", ours=False, limit=300):
        words = [w for w in (q or "").lower().replace("ё", "е").split() if w]
        out = [
            p
            for hay, p in self._catalog()
            if (not site or p["site"] == site) and (not ours or p["is_ours"]) and all(w in hay for w in words)
        ]
        return {"items": out[:limit], "total": len(out)}

    def product(self, key):
        row = self.con.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
        if not row:
            return {"error": "товар не найден"}
        overrides, decisions = db.kind_overrides(self.con), db.decisions(self.con)
        brand = config.load().get("brand_words") or []
        p = self._enrich(dict(row), overrides, decisions, brand)
        since = time.time() - 365 * 86400
        p["history"] = db.history_of(self.con, key, since)
        p["points"] = db.market_points(self.con, key, since)
        p["decisions"] = sorted(decisions.get(key, set()))
        p["pack_manual"] = key in db.pack_overrides(self.con)
        p["crosscheck"] = [
            dict(r)
            for r in self.con.execute(
                "SELECT ts, feed_price, site_price FROM crosscheck WHERE key=? ORDER BY ts DESC LIMIT 10", (key,)
            )
        ]
        # аналоги из того же сбора: тот же тип, без противоречащих признаков, та же единица
        analogs = []
        if p["attrs"] and p.get("last_run"):
            for r in self.con.execute(
                "SELECT * FROM products WHERE site=? AND last_run=? AND key!=?", (p["site"], p["last_run"], key)
            ):
                c = self._enrich(dict(r), overrides, decisions, brand)
                same_type = (c["attrs"] or {}).get("type") == p["attrs"].get("type")
                if same_type and c.get("pack_unit") == p.get("pack_unit") and c.get("per_unit"):
                    c["analog"] = kinds.compatible(p["attrs"], c["attrs"])
                    c.pop("attrs", None)
                    analogs.append(c)
        # выбросы — как в анализе: цена за единицу в N раз от середины аналогов
        ratio = float(config.load()["review"].get("unit_ratio") or 3.0)
        base = sorted(c["per_unit"] for c in analogs if c["analog"] and not c["excluded"] and not c["is_ours"])
        med = (
            (base[len(base) // 2] if len(base) % 2 else (base[len(base) // 2 - 1] + base[len(base) // 2]) / 2)
            if base
            else None
        )
        for c in analogs:
            k = (c["per_unit"] / med if c["per_unit"] > med else med / c["per_unit"]) if med and len(base) >= 3 else 1
            c["outlier"] = round(k, 1) if k >= ratio else None
        analogs.sort(key=lambda c: (not c["analog"], c["outlier"] is not None, c["per_unit"]))
        p["analogs"] = analogs[:40]
        p.pop("attrs", None)
        return p

    def decisions_list(self):
        out = []
        for d in db.decisions_list(self.con):
            d["site_title"] = sites.SITES.get(d.get("site") or "", {}).get("title", "")
            out.append(d)
        return out

    # ------------------------------------------------------------ проверка связи

    def check(self, site, edge=False):
        s = config.load()
        conf = s["sites"][site]
        out = {"site": site, "feed": None, "edge": None}
        if conf.get("feed"):
            try:
                path, meta = feeds.fetch(conf["feed"], config.FEEDS_DIR, site, force=True, min_interval_h=0)
                sample: list[dict[str, Any]] = []

                def want(o, feed):
                    if len(sample) < 5 and o.get("price"):
                        sample.append({"name": o["name"], "price": o["price"], "url": o["url"]})
                    return False

                feed = feeds.read(path, site, want)
                db.save_categories(self.con, site, feed.categories)
                out["feed"] = {
                    "ok": True,
                    "format": feed.format,
                    "total": feed.total,
                    "categories": len(feed.categories),
                    "date": feed.date,
                    "no_price": feed.no_price,
                    "tracker_links": feed.tracker_links,
                    "sample": sample,
                    "status": meta.get("status"),
                }
            except feeds.FeedError as e:
                out["feed"] = {"ok": False, "error": str(e)}
            except guard.Blocked:
                out["feed"] = {"ok": False, "error": "Сервер фида просит ходить реже (429). Попробуйте через час."}
        if edge:
            out["edge"] = self.check_edge(site, s)
        return out

    def check_edge(self, site, s):
        """Открывает главную сети в окне сбора: город, проверка браузера, robots.txt для раздела каталога."""
        st = collect.Status()
        v = Visitor(s, st, interactive=True)
        try:
            v.open()
            home = sites.SITES[site]["home"]
            v.allowed(home + "catalogue/" if site == "lemanapro" else home + "catalog/")
            v.tab.show_window(True)
            v._open(home)
            page = v.tab.evaluate(extract.PAGE_SCRIPT, timeout=40) or {}
            robots = "прочитан" if v.robots.get(sites.origin(home)) else "не прочитан (открываем только отслеживаемое)"
            return {
                "ok": not extract.is_check_page(page),
                "city": extract.city_of(page) or sites.city_from_url(home),
                "check_page": extract.is_check_page(page),
                "title": page.get("title", ""),
                "robots": robots,
                "hint": "Окно сбора открыто: если нужно, выберите в нём город и магазин — сайт запомнит выбор.",
            }
        except cdp.BrowserError as e:
            return {"ok": False, "error": str(e)}
        except guard.Blocked as b:
            return {"ok": False, "error": f"Сайт ответил отказом ({b.detail or b.reason}). Попробуйте позже."}
        finally:
            if v.is_open:
                v.tab.ws.close()  # окно оставляем открытым — человек может выбрать город


def open_path(path):
    if not path or not os.path.exists(path):
        return False
    if sys.platform.startswith("win"):
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ---------------------------------------------------------------- HTTP


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            return secrets.compare_digest(self.headers.get("X-Token", ""), app.token)

        def _guarded(self, handle):
            """Неожиданная ошибка — ответ 500 с понятным текстом и запись в журнал, а не оборванное соединение."""
            try:
                handle()
            except (BrokenPipeError, ConnectionResetError):
                pass  # окно закрыли, не дождавшись ответа
            except Exception as e:
                log.error(
                    "ui.error",
                    f"Ошибка обработки {self.command} {urlsplit(self.path).path}: {e.__class__.__name__}: {e}",
                    trace=traceback.format_exc()[-3000:],
                )
                try:
                    self._send(500, {"error": f"внутренняя ошибка: {e}. Подробности — в журнале"})
                except OSError:
                    pass

        def do_GET(self):
            self._guarded(self._get)

        def do_POST(self):
            self._guarded(self._post)

        def _get(self):
            u = urlsplit(self.path)
            if u.path == "/":
                if parse_qs(u.query).get("t", [""])[0] != app.token:
                    return self._send(
                        403, "Откройте приложение через «Запустить.cmd»".encode(), "text/plain; charset=utf-8"
                    )
                with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
                    html = f.read().replace("__TOKEN__", app.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if not self._authorized():
                return self._send(403, {"error": "нет ключа"})
            app.last_ping = time.time()
            if u.path == "/api/state":
                return self._send(200, app.state())
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/categories":
                return self._send(200, app.find_categories(q.get("site", ""), q.get("q", "")))
            if u.path == "/api/products":
                return self._send(200, app.products(q.get("q", ""), q.get("site", ""), q.get("ours") == "1"))
            if u.path == "/api/product":
                return self._send(200, app.product(q.get("key", "")))
            if u.path == "/api/decisions":
                return self._send(200, app.decisions_list())
            if u.path == "/api/journal":
                run_id = int(q["run"]) if q.get("run", "").isdigit() else None
                return self._send(200, {"records": log.tail(int(q.get("n") or 200), q.get("level") or "INFO", run_id)})
            if u.path == "/api/condition":
                return self._send(200, app.condition())
            if u.path == "/api/health":
                return self._send(200, health.check(app.con, config.load()))
            if u.path == "/api/kinds":
                return self._send(200, kinds.all_labels())
            if u.path == "/api/spot":
                return self._send(200, spot.view(app.con, config.load()))
            return self._send(404, {"error": "нет такого адреса"})

        def _post(self):
            if not self._authorized():
                return self._send(403, {"error": "нет ключа"})
            app.last_ping = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except ValueError:
                return self._send(400, {"error": "плохой запрос"})
            p = urlsplit(self.path).path
            try:
                if p == "/api/run":
                    return self._send(200, app.start_run(bool(body.get("force_feed"))))
                if p == "/api/breaker_reset":
                    guard.reset(app.con, body["site"], body["source"])
                    app._health = (0.0, None)
                    return self._send(200, {"ok": True})
                if p == "/api/backup":
                    path = backup.make(app.con, "вручную")
                    return self._send(200, {"ok": bool(path), "path": path})
                if p == "/api/cancel":
                    app.status.cancel()
                    return self._send(200, {"ok": True})
                if p == "/api/human_done":
                    app.status.human_done()
                    return self._send(200, {"ok": True})
                if p == "/api/track":
                    return self._send(200, app.add_links(body.get("site"), body.get("text")))
                if p == "/api/track_category":
                    db.add_tracked(app.con, body["site"], "category", str(body["id"]), body.get("name") or "")
                    return self._send(200, {"ok": True})
                if p == "/api/untrack":
                    db.remove_tracked(app.con, int(body["id"]))
                    return self._send(200, {"ok": True})
                if p == "/api/settings":
                    return self._send(200, config.save(body["settings"]))
                if p == "/api/kind":
                    db.set_kind(app.con, body["key"], body.get("kind") or "")
                    return self._send(200, {"ok": True, "kind": kinds.detect(body.get("kind") or "")})
                if p == "/api/pack":
                    try:
                        qty, unit = db.set_pack(app.con, body["key"], body.get("pack") or "")
                    except ValueError as e:
                        return self._send(200, {"ok": False, "error": str(e)})
                    return self._send(200, {"ok": True, "pack": units.pack_label(qty, unit)})
                if p == "/api/spot_new":
                    keys = spot.new_sample(app.con, config.load(), int(body.get("n") or 30))
                    return self._send(200, {"ok": True, "n": len(keys)})
                if p == "/api/spot":
                    try:
                        if body.get("verdict") == "undo":
                            return self._send(200, spot.undo(app.con, body["key"]))
                        return self._send(
                            200, spot.mark(app.con, config.load(), body["key"], body["verdict"], body.get("value", ""))
                        )
                    except ValueError as e:
                        return self._send(200, {"ok": False, "error": str(e)})
                if p == "/api/decide":
                    db.decide(app.con, body["key"], body["kind"], body.get("note", ""))
                    return self._send(200, {"ok": True})
                if p == "/api/check":
                    return self._send(200, app.check(body["site"], bool(body.get("edge"))))
                if p == "/api/open":
                    target = body.get("path") or ""
                    if body.get("what") == "folder":
                        target = config.report_dir(config.load())
                    elif body.get("what") == "samples":
                        target = config.SAMPLES_DIR
                    elif body.get("what") in ("failures", "backups", "logs"):
                        target = {"failures": dumps.folder(), "backups": backup.folder(), "logs": config.LOGS_DIR}[
                            body["what"]
                        ]
                        os.makedirs(target, exist_ok=True)
                    elif body.get("what") == "dump":
                        target = os.path.realpath(target)
                        if os.path.dirname(target) != os.path.realpath(dumps.folder()):
                            return self._send(400, {"error": "можно открыть только папку дампа"})
                    else:
                        folders = {
                            os.path.realpath(d)
                            for d in (config.load().get("report_dir"), config.default_report_dir())
                            if d
                        }
                        target = os.path.realpath(target) if target else ""
                        if not (target.endswith(".xlsx") and os.path.dirname(target) in folders):
                            return self._send(400, {"error": "можно открыть только отчёт"})
                    return self._send(200, {"ok": open_path(target)})
                if p == "/api/schedule":
                    return self._send(
                        200,
                        schedule.set_schedule(
                            bool(body.get("enabled")),
                            body.get("at") or "08:30",
                            bool(body.get("watchdog", True)),
                            bool(body.get("wake")),
                        ),
                    )
                if p == "/api/selftest":
                    return self._send(200, selftest.run(config.load(), app.con))
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": f"плохой запрос: {e}"})
            return self._send(404, {"error": "нет такого адреса"})

    return H


def serve(app, port=0):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
