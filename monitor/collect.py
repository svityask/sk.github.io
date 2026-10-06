"""Один сбор: фид (основной путь) → Edge для пробелов (запасной) → база → анализ → отчёт Excel.

Надёжность:
  • один сбор за раз — блокировка в базе с «сердцебиением» (окно и расписание не пишут одновременно);
  • у каждого источника (сеть × фид/сайт) — предохранитель guard: 429 и капча ставят его на паузу, сбои копятся;
  • запись цен сети — одна транзакция: либо всё, либо ничего; история не задваивается (history.run_id);
  • метрики по источнику — в таблицу metrics; журнал — JSON (log.py); после удачного сбора — резервная копия.
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
import traceback
import uuid
from typing import Any

from . import analysis, backup, cdp, config, db, dumps, extract, feeds, guard, kinds, log, report, sites, units
from .metrics import Meter
from .visitor import Visitor

LOCK = "collect"
HEARTBEAT_S = 30


class Status:
    """Состояние сбора для интерфейса. Работает и без интерфейса (сбор по расписанию)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.text = ""
        self.log = []
        self.running = False
        self.human = None  # текст просьбы к человеку
        self._human_done = threading.Event()
        self._cancel = threading.Event()
        self.result = None
        self.on_progress = None  # вызывается при каждом шаге (сердцебиение блокировки)

    def set(self, text):
        with self.lock:
            self.text = text
            self.log.append((time.time(), text))
            self.log = self.log[-300:]
        if self.on_progress:
            self.on_progress()

    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()
        self._human_done.set()

    def human_done(self):
        self._human_done.set()

    def ask_human(self, message, check, timeout):
        self._human_done.clear()
        with self.lock:
            self.human = message
        self.set(message)
        deadline = time.time() + timeout
        ok = False
        try:
            while time.time() < deadline and not self.cancelled():
                if self._human_done.wait(2):
                    ok = True
                    break
                if self.on_progress:
                    self.on_progress()
                try:
                    if check():
                        ok = True
                        break
                except cdp.BrowserError:
                    pass
        finally:
            with self.lock:
                self.human = None
        return ok

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "text": self.text,
                "human": self.human,
                "log": [t for _, t in self.log[-12:]],
                "result": self.result,
            }


def run(settings, status=None, interactive=True, force_feed=False, con=None) -> dict[str, Any]:
    """Точка входа. Если уже идёт другой сбор — ничего не делает и возвращает {'skipped': True}."""
    status = status or Status()
    status.running = True
    own_con = con is None
    con = con or db.connect(config.DB_PATH)
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        holder = db.acquire_lock(con, LOCK, owner)
        if holder:
            since = time.strftime("%H:%M", time.localtime(holder["started"]))
            msg = f"Уже идёт другой сбор (начат в {since}) — этот не запускаем"
            log.warning("run.skipped", msg, holder_pid=holder["pid"])
            status.set(msg)
            status.result = {"skipped": True, "warnings": [msg], "sites": {}, "changes": [], "assortment": []}
            return status.result
        try:
            return Collector(settings, status, interactive, force_feed, con, owner).run()
        finally:
            db.release_lock(con, LOCK, owner)
    finally:
        status.running = False
        if own_con:
            con.close()


class Collector:
    def __init__(self, settings, status, interactive, force_feed, con, owner):
        self.settings = settings
        self.status = status
        self.interactive = interactive
        self.force_feed = force_feed
        self.con: sqlite3.Connection = con
        self.owner = owner
        self.brand = settings.get("brand_words") or []
        self.groups: dict[int, str] = {}
        self.missing: list[tuple[str, str, str]] = []
        self.pairs: list[tuple[str, str, float, float]] = []  # (сеть, товар, цена фида, цена сайта)
        self.meters: list[Meter] = []
        self.visitor: Visitor | None = None
        self._beat = 0.0

    # ------------------------------------------------------------ общее

    def _heartbeat(self):
        if time.time() - self._beat >= HEARTBEAT_S:
            self._beat = time.time()
            db.heartbeat(self.con, LOCK, self.owner)

    def warn(self, text, event="run.warning", **fields):
        self.summary["warnings"].append(text)
        log.warning(event, text, **fields)

    def run(self) -> dict[str, Any]:
        con = self.con
        self.run_id = db.start_run(con)
        recovered = db.recover_interrupted(con, keep_id=self.run_id)
        self.started = time.time()
        self.summary: dict[str, Any] = {
            "sites": {},
            "warnings": [],
            "changes": [],
            "assortment": [],
            "run_id": self.run_id,
        }
        self.status.on_progress = self._heartbeat
        with log.context(run_id=self.run_id, mode="окно" if self.interactive else "расписание"):
            log.info("run.start", f"Сбор №{self.run_id} начат", recovered=recovered or None)
            try:
                for site, conf in self.settings["sites"].items():
                    if conf.get("enabled"):
                        with log.context(site=site):
                            self._site_safe(site, conf)
                self._close_visitor()
                result = self._analyse_and_report()
                self._after_success()
                return result
            except Exception as e:
                s = self.summary
                s["error"] = f"{e.__class__.__name__}: {e}"
                s["trace"] = traceback.format_exc()[-3000:]
                db.finish_run(con, self.run_id, "ошибка", s)
                self.status.set(f"Сбор прерван: {e}")
                self.status.result = s
                log.error("run.error", f"Сбор №{self.run_id} прерван: {s['error']}", trace=s["trace"])
                return s
            finally:
                self._close_visitor()
                self.status.on_progress = None
                try:
                    db.save_metrics(con, self.run_id, [m.row() for m in self.meters])
                except sqlite3.Error as e:
                    log.error("metrics.failed", f"Метрики не записались: {e}")

    def _close_visitor(self):
        if self.visitor:
            self.visitor.close()
            self.visitor = None

    # ------------------------------------------------------------ сеть

    def _site_safe(self, site, conf):
        """Неожиданная ошибка в одной сети не отменяет сбор по другой: её цены просто не записываются."""
        try:
            self._site(site, conf)
        except (sqlite3.DatabaseError, MemoryError):
            raise  # с базой или памятью беда — дальше собирать бессмысленно
        except Exception as e:
            title = sites.SITES[site]["title"]
            info = self.summary["sites"].setdefault(site, {"title": title})
            info["error"] = f"{e.__class__.__name__}: {e}"
            self._close_visitor()  # окно могло остаться в непонятном состоянии — следующая сеть откроет новое
            self.warn(
                f"{title}: сбор по сети прерван ошибкой ({e.__class__.__name__}: {e}) — цены сети не записаны",
                "site.error",
                trace=traceback.format_exc()[-3000:],
            )

    def _site(self, site, conf):
        con = self.con
        title = sites.SITES[site]["title"]
        entries = db.tracked(con, site)
        if not entries:
            self.summary["sites"][site] = {"title": title, "note": "ничего не отслеживается"}
            return
        info: dict[str, Any] = {"title": title, "feed": None, "edge": None}
        self.summary["sites"][site] = info
        cats = db.categories(con, site)
        for e in entries:
            self.groups[e["id"]] = _group_title(e, cats)
        cat_entries = {e["ref"]: e for e in entries if e["kind"] == "category"}
        prod_by_key = {}
        for e in entries:
            if e["kind"] == "product":
                k = sites.product_key(site, url=e["ref"])
                if k:
                    prod_by_key[k] = e
        found: dict[str, dict] = {}

        feed_ok = self._feed(site, conf, info, entries, cat_entries, prod_by_key, found) if conf.get("feed") else False

        # Edge — только пробелы
        sections = [(e["id"], e["ref"], self.groups[e["id"]]) for e in entries if e["kind"] == "section"]
        gaps = [(e["id"], e["ref"], sites.code_from_url(e["ref"])) for k, e in prod_by_key.items() if k not in found]
        if not feed_ok and cat_entries:
            # фида нет: обновляем по сайту уже известные товары этих категорий (наши — первыми)
            known = con.execute(
                f"SELECT key,url,code,group_id,is_ours FROM products WHERE site=? AND group_id IN "
                f"({','.join('?' * len(cat_entries))}) AND url!='' ORDER BY is_ours DESC, last_seen DESC",
                (site, *[e["id"] for e in cat_entries.values()]),
            ).fetchall()
            gaps += [(r["group_id"], r["url"], r["code"]) for r in known if r["key"] not in prod_by_key]
            if known:
                self.warn(
                    f"{title}: фид недоступен — известные товары обновляются по сайту, сколько позволит лимит страниц"
                )
        edge_city, feed_city = conf.get("edge_city") or "", conf.get("feed_city") or ""
        checks = _pick_checks(found, self.settings, self.run_id) if feed_ok and conf.get("edge_enabled") else []
        if (sections or gaps or checks) and conf.get("edge_enabled"):
            if feed_ok and feed_city and edge_city and not extract.same_city(feed_city, edge_city):
                self.warn(
                    f"{title}: город фида ({feed_city}) и окна Edge ({edge_city}) разные — "
                    f"дособирать с сайта не стали, чтобы не смешать цены"
                )
            else:
                self._edge(site, info, sections, gaps, checks, edge_city or feed_city, found, prod_by_key)
        elif gaps and feed_ok:
            for _gid, url, code in gaps:
                k = sites.product_key(site, code=code, url=url)
                if k in prod_by_key:
                    self.missing.append((site, url, prod_by_key[k]["title"]))

        self._write(site, info, found, feed_ok, cat_entries, prod_by_key)

    # ------------------------------------------------------------ 1. фид

    def _feed(self, site, conf, info, entries, cat_entries, prod_by_key, found) -> bool:
        con, title = self.con, sites.SITES[site]["title"]
        m = Meter(site, "feed")
        self.meters.append(m)
        allowed, b = guard.allow(con, site, "feed")
        if not allowed:
            self.warn(
                f"{title}: фид на паузе до {_hm(b['until'])} ({b['reason']}) — берём прошлый файл, если есть",
                "feed.paused",
            )
        self.status.set(f"{title}: загружаю фид")
        path = None
        try:
            kw = dict(force=self.force_feed, min_interval_h=self.settings.get("feed_interval_h") or 6, meter=m)
            try:
                path, meta = feeds.fetch(conf["feed"], config.FEEDS_DIR, site, allow_network=allowed, **kw)
            except guard.Blocked as bl:
                guard.blocked(con, site, "feed", bl.detail or bl.reason, bl.retry_after)
                m.outcome = "blocked"
                self.warn(
                    f"{title}: сервер фида просит паузу (429) — следующая загрузка не раньше чем через час",
                    "feed.blocked",
                )
                path, meta = feeds.fetch(conf["feed"], config.FEEDS_DIR, site, allow_network=False, **kw)
            else:
                if allowed and meta.get("stale"):
                    guard.failure(con, site, "feed", meta.get("status") or "фид не скачался")
                    m.outcome = "partial"
                    self.warn(f"{title}: {meta.get('status')}", "feed.stale")
                elif allowed and meta.get("status") in ("скачан", "не изменился", "файл"):
                    guard.success(con, site, "feed")
            cat_entries_local = cat_entries

            def want(offer, feed):
                gid = None
                for cid in feed.chain(offer.get("category_id")):
                    if cid in cat_entries_local:
                        gid = cat_entries_local[cid]["id"]
                        break
                pe = prod_by_key.get(offer["key"])
                if gid is None and pe is None:
                    return False
                offer["group_id"] = gid if gid is not None else pe["id"]
                return True

            self.status.set(f"{title}: читаю фид")
            feed = feeds.read(path, site, want)
            db.save_categories(con, site, feed.categories)
            for e in entries:
                self.groups[e["id"]] = _group_title(e, feed.categories)
            for o in feed.offers:
                if o["price"] is None or o["currency"] != "RUB":
                    continue
                o.update(source="feed", city=conf.get("feed_city") or None, via="фид")
                found[o["key"]] = o
            age_h = (time.time() - (feed.date or meta.get("fetched_at") or time.time())) / 3600
            info["feed"] = {
                "status": meta.get("status"),
                "date": feed.date,
                "total": feed.total,
                "taken": len(feed.offers),
                "format": feed.format,
                "age_h": round(age_h, 1),
                "categories": len(feed.categories),
            }
            m.prices = len(found)
            log.info(
                "feed.read",
                f"Фид прочитан: {feed.total} позиций, взято {len(feed.offers)}",
                total=feed.total,
                taken=len(feed.offers),
                format=feed.format,
                age_h=round(age_h, 1),
            )
            if age_h > float(self.settings["review"].get("feed_age_h") or 36):
                self.warn(
                    f"{title}: фиду {age_h:.0f} ч — сеть давно его не обновляла, цены могут отставать", "feed.old"
                )
            if cat_entries and not feed.offers:
                m.empty += 1
                self.warn(f"{title}: в фиде нет ни одного товара из выбранных категорий", "feed.empty")
            return True
        except feeds.FeedError as e:
            m.errors += 1
            m.outcome = "failed"
            info["feed"] = {"error": str(e)}
            self.warn(f"{title}: {e}", "feed.failed")
            if path:  # скачали, но не прочитали — сохраним начало файла для разбора
                dumps.save(
                    site,
                    "feed-broken",
                    url=conf["feed"] if conf["feed"].startswith("http") else "",
                    feed_path=path,
                    extra={"error": str(e)},
                )
            if allowed:
                guard.failure(con, site, "feed", str(e))
            return False
        finally:
            m.finish()

    # ------------------------------------------------------------ 2. сайт

    def _edge(self, site, info, sections, gaps, checks, expect_city, found, prod_by_key):
        con, title = self.con, sites.SITES[site]["title"]
        allowed, b = guard.allow(con, site, "edge")
        if not allowed:
            self.meters.append(Meter(site, "edge").finish("skipped"))
            info["edge"] = {"error": f"на паузе до {_hm(b['until'])}: {b['reason']}"}
            self.warn(f"{title} (сайт): на паузе до {_hm(b['until'])} — {b['reason']}. Пропускаем", "edge.paused")
            return
        m = Meter(site, "edge")
        self.meters.append(m)
        try:
            if self.visitor is None:
                self.status.set("Открываю окно Edge для сбора")
                self.visitor = Visitor(self.settings, self.status, self.interactive)
                self.visitor.open()
            res = self.visitor.run_site(
                site,
                sections,
                gaps,
                expect_city=expect_city,
                checks=checks,
                checks_n=int(self.settings.get("crosscheck", {}).get("n") or 0),
                meter=m,
            )
        except cdp.BrowserError as e:  # окно не открылось или его закрыли — это не сбой сайта
            m.errors += 1
            m.finish("failed")
            info["edge"] = {"error": str(e)}
            self.warn(f"{title} (сайт): {e}", "edge.browser")
            return
        if res["blocked"]:
            bl = res["blocked"]
            until = guard.blocked(con, site, "edge", bl["detail"] or bl["reason"], bl["retry_after"])
            m.outcome = "blocked"
            self.warn(
                f"{title} (сайт): {bl['detail'] or bl['reason']} — сеть на паузе до {_hm(until)}",
                "edge.blocked",
                reason=bl["reason"],
            )
        elif res["failed"]:
            guard.failure(con, site, "edge", res["stopped"] or "сайт не открылся")
            m.outcome = "failed"
            self.warn(f"{title} (сайт): {res['stopped']}", "edge.failed")
        else:
            if m.pages:
                guard.success(con, site, "edge")
            m.outcome = "partial" if (m.errors or m.empty or res["stopped"]) else "ok"
            if res["stopped"]:
                self.warn(f"{title} (сайт): {res['stopped']}", "edge.stopped")
        m.finish()
        info["edge"] = {
            "pages": res["pages"],
            "taken": len(res["items"]),
            "via": sorted(res["via"]),
            "city": res["city"],
            "stopped": res["stopped"],
            "notes": res["notes"][:30],
        }
        log.info(
            "edge.done",
            f"Сайт: страниц {res['pages']}, цен {len(res['items'])}",
            pages=res["pages"],
            prices=len(res["items"]),
            outcome=m.outcome,
        )
        for it in res["checks"]:
            k = it["check_key"]
            if it.get("price") and k in found and found[k].get("source") == "feed":
                self.pairs.append((site, k, found[k]["price"], it["price"]))
        for it in res["items"]:
            if not it.get("price"):
                continue
            key = sites.product_key(site, code=it.get("code"), url=it.get("url"))
            if key in found and found[key].get("source") == "feed":
                self.pairs.append((site, key, found[key]["price"], it["price"]))  # сверка бесплатно
            if not key or key in found:
                continue  # фид главнее
            it.update(key=key, source="edge", city=res["city"] or expect_city or None, category_id=None, params={})
            found[key] = it
        if not res["stopped"]:
            for _gid, url, code in gaps:
                k = sites.product_key(site, code=code, url=url)
                if k in prod_by_key and k not in found:
                    self.missing.append((site, url, prod_by_key[k]["title"]))

    # ------------------------------------------------------------ 3. запись (одна транзакция на сеть)

    def _write(self, site, info, found, feed_ok, cat_entries, prod_by_key):
        con, title = self.con, sites.SITES[site]["title"]
        self.status.set(f"{title}: записываю {len(found)} цен")
        uniq: dict[str, tuple] = {}
        for x in self.pairs:
            if x[0] == site:
                uniq.setdefault(x[1], x)  # товар мог встретиться на двух страницах раздела
        site_pairs = list(uniq.values())
        self.pairs = [x for x in self.pairs if x[0] != site] + site_pairs

        def write():
            with db.transaction(con):
                avail_events = []
                titles = {}
                for key, it in found.items():
                    qty, unit = units.pack_of(it.get("name"), it.get("params"), kinds.pack_unit(it.get("name")))
                    ours, _doubt = analysis.is_ours(it.get("name"), it.get("vendor"), self.brand)
                    item = {
                        "key": key,
                        "site": site,
                        "code": it.get("code"),
                        "name": it.get("name"),
                        "url": it.get("url"),
                        "vendor": it.get("vendor"),
                        "category_id": it.get("category_id"),
                        "group_id": it.get("group_id"),
                        "pack_qty": qty,
                        "pack_unit": unit,
                        "is_ours": ours,
                        "price": it["price"],
                        "old_price": it.get("old_price"),
                        "available": it.get("available"),
                        "source": it["source"],
                        "city": it.get("city"),
                    }
                    ch = db.record(con, self.run_id, item, ts=self.started)
                    if (
                        ch["avail_from"] is not None
                        and ch["avail_to"] is not None
                        and ch["avail_from"] != ch["avail_to"]
                    ):
                        avail_events.append(("back" if ch["avail_to"] else "out", key))
                    pe = prod_by_key.get(key)
                    if pe and not pe["title"] and it.get("name"):
                        con.execute("UPDATE tracked SET title=? WHERE id=?", (it["name"][:200], pe["id"]))
                        titles[pe["id"]] = it["name"][:200]
                events = _assortment(con, site, self.run_id, self.started, feed_ok, cat_entries, avail_events)
                if feed_ok:
                    db.mark_read(con, self.run_id, site)
                if site_pairs:
                    db.save_crosscheck(con, self.run_id, site, [(k, f, sp) for _, k, f, sp in site_pairs])
            return events, titles

        def on_retry(n, e, delay):
            log.warning("db.retry", f"База занята ({e}), повтор записи через {delay:.0f} с")

        events, titles = guard.retry(
            write, attempts=3, base=2.0, retry_on=(sqlite3.OperationalError,), on_retry=on_retry
        )
        self.groups.update(titles)
        info["prices"] = len(found)
        self.summary["assortment"] += events
        if site_pairs:
            info["crosscheck"] = _crosscheck_summary(site_pairs, found)
        log.info("site.written", f"{title}: записано цен {len(found)}", prices=len(found), assortment=len(events))

    # ------------------------------------------------------------ 4. анализ и отчёт

    def _analyse_and_report(self):
        con, s = self.con, self.summary
        self.status.set("Сравниваю с рынком")
        products = db.products_of_run(con, self.run_id)
        for p in products:
            p["is_ours"], p["brand_doubt"] = analysis.is_ours(p.get("name"), p.get("vendor"), self.brand)
            if p["prev_price"] and p["price"] != p["prev_price"]:
                last = con.execute(
                    "SELECT run_id FROM history WHERE key=? ORDER BY ts DESC, id DESC LIMIT 1", (p["key"],)
                ).fetchone()
                if last and last["run_id"] == self.run_id:
                    p["changed_now"] = True
                    s["changes"].append(
                        {
                            "key": p["key"],
                            "site": p["site"],
                            "name": p["name"],
                            "url": p["url"],
                            "old": p["prev_price"],
                            "new": p["price"],
                            "source": p["source"],
                            "is_ours": p["is_ours"],
                            "text": analysis.change_text(
                                p["name"], p["prev_price"], p["price"], sites.SITES[p["site"]]["title"], p["source"]
                            ),
                        }
                    )
        decisions = db.decisions(con)
        market, review, stats = analysis.analyse(
            products, self.groups, self.settings, decisions, self.missing, db.kind_overrides(con)
        )
        warn = float(self.settings.get("crosscheck", {}).get("warn_pct") or 10)
        for site, key, fp, sp in self.pairs:
            diff = (sp - fp) / fp * 100
            sig = f"feedgap:{round(fp)}:{round(sp)}"
            if (
                abs(diff) >= warn
                and f"accept:{sig}" not in decisions.get(key, set())
                and "exclude" not in decisions.get(key, set())
            ):
                prod: dict[str, Any] = next((x for x in products if x["key"] == key), {})
                review.append(
                    {
                        "key": key,
                        "site": site,
                        "name": prod.get("name") or key,
                        "url": prod.get("url") or "",
                        "price": fp,
                        "kind": "feedgap",
                        "signature": sig,
                        "reason": f"Фид: {analysis.rub(fp)} ₽, на сайте: {analysis.rub(sp)} ₽ "
                        f"({'+' if diff > 0 else '−'}{abs(diff):.0f} %) — фид расходится с полкой",
                    }
                )
        db.save_market_points(con, self.run_id, market)
        names = {p["key"]: p for p in products}
        for ev in s["assortment"]:
            ev["is_ours"] = bool(names.get(ev["key"], {}).get("is_ours", ev.get("is_ours")))
        s["review"] = len(review)
        s["review_items"] = review[:500]
        s["market"] = [
            {
                k: r[k]
                for k in (
                    "key",
                    "site",
                    "group",
                    "kind",
                    "level",
                    "name",
                    "url",
                    "pack",
                    "price",
                    "per_unit",
                    "unit",
                    "median",
                    "deviation",
                    "cheaper_than",
                    "count",
                    "min",
                    "min_name",
                    "min_url",
                )
            }
            for r in market
        ]
        s["prices"] = len(products)
        s["metrics"] = [m.row() for m in self.meters]
        if not products and not review:
            s["warnings"].append("Цен нет: выберите, что отслеживать, и задайте фид в настройках")
            db.finish_run(con, self.run_id, "без цен", s)
            self.status.set("Цен нет — нечего класть в отчёт")
            self.status.result = s
            self._log_result()
            return s
        self.status.set("Готовлю отчёт Excel")
        s["report"] = report.build(self.settings, con, self.run_id, products, market, review, stats, s, self.groups)
        s["seconds"] = round(time.time() - self.started)
        db.finish_run(con, self.run_id, "готово", s)
        self.status.set(f"Готово: {len(products)} цен, изменений {len(s['changes'])}, на проверку {len(review)}")
        self.status.result = s
        self._log_result()
        return s

    def _after_success(self):
        if self.summary.get("prices") and backup.due(20):
            try:
                self.status.set("Делаю резервную копию базы")
                backup.make(self.con)
            except (OSError, sqlite3.Error) as e:
                log.error("backup.failed", f"Резервная копия не сделана: {e}")
        dumps.prune()

    def _log_result(self):
        s = self.summary
        totals = {
            f: sum(getattr(m, f) for m in self.meters)
            for f in ("pages", "errors", "retries", "http_429", "captcha", "empty")
        }
        log.info(
            "run.finish",
            f"Сбор №{self.run_id}: цен {s.get('prices', 0)}, изменений {len(s['changes'])}, "
            f"ассортимент {len(s['assortment'])}, на проверку {s.get('review', 0)}",
            seconds=round(time.time() - self.started),
            prices=s.get("prices", 0),
            report=s.get("report"),
            warnings=len(s["warnings"]),
            **totals,
        )


# ---------------------------------------------------------------- помощники


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "—"


def _group_title(entry, cats):
    if entry["title"]:
        return entry["title"]
    if entry["kind"] == "category" and entry["ref"] in cats:
        return cats[entry["ref"]]["name"]
    return entry["ref"]


def _pick_checks(found, settings, run_id):
    """Карточки для сверки фида с полкой: наши — всегда, остальные — по кругу (разные в каждом сборе)."""
    n = int(settings.get("crosscheck", {}).get("n") or 0)
    feed_items = [o for o in found.values() if o.get("source") == "feed" and o.get("url")]
    if n <= 0 or not feed_items:
        return []
    brand = [w.lower() for w in settings.get("brand_words") or []]
    ours = [
        o
        for o in feed_items
        if any(w in ((o.get("vendor") or "") + " " + (o.get("name") or "")).lower() for w in brand)
    ]
    others = [o for o in feed_items if o not in ours]
    rnd = random.Random(run_id)
    ours = sorted(ours, key=lambda o: o["key"])
    rnd.shuffle(ours)
    pick = ours[:n]  # наши — в первую очередь
    rest = sorted(others, key=lambda o: o["key"])
    rnd.shuffle(rest)
    pick += rest[: 3 * n - len(pick)]  # с запасом: часть уже окажется в разделах, часть не откроется
    return [(o["key"], o["url"], o.get("code")) for o in pick]


def _crosscheck_summary(site_pairs, found):
    diffs = [(sp - fp) / fp * 100 for _, _, fp, sp in site_pairs]
    return {
        "n": len(diffs),
        "avg_abs_pct": round(sum(abs(d) for d in diffs) / len(diffs), 1),
        "avg_pct": round(sum(diffs) / len(diffs), 1),
        "over5": sum(1 for d in diffs if abs(d) >= 5),
        "same": sum(1 for d in diffs if abs(d) < 0.5),
        "items": [
            {
                "key": k,
                "name": (found.get(k) or {}).get("name") or k,
                "url": (found.get(k) or {}).get("url"),
                "feed": fp,
                "site": sp,
                "pct": round((sp - fp) / fp * 100, 1),
            }
            for _, k, fp, sp in site_pairs
        ],
    }


def _assortment(con, site, run_id, started, feed_ok, cat_entries, avail_events):
    """Появился в фиде, пропал из фида, закончился, вернулся в наличие."""
    out = []

    def ev(kind, row):
        out.append(
            {
                "type": kind,
                "key": row["key"],
                "site": site,
                "name": row["name"],
                "url": row["url"],
                "price": row["price"],
                "group_id": row["group_id"],
                "is_ours": bool(row["is_ours"]),
            }
        )

    for kind, key in avail_events:
        row = con.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
        if row:
            ev(kind, row)
    prev = db.previous_read(con, site, run_id) if feed_ok and cat_entries else None
    if prev:
        # категории, которые отслеживались ещё при прошлом чтении, — иначе всё «новое» окажется новым
        old_groups = [e["id"] for e in cat_entries.values() if e["added"] < prev["ts"]]
        if old_groups:
            marks = ",".join("?" * len(old_groups))
            for row in con.execute(
                f"SELECT * FROM products WHERE site=? AND first_seen>=? AND group_id IN ({marks})",
                (site, started, *old_groups),
            ):
                ev("new", row)
            for row in con.execute(
                f"SELECT * FROM products WHERE site=? AND last_run=? AND source='feed' AND group_id IN ({marks})",
                (site, prev["run_id"], *old_groups),
            ):
                ev("gone", row)
    return out
