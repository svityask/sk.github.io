"""Надёжность: журнал, предохранитель, повторы, 429, блокировка, транзакции, миграции, метрики, бэкапы, здоровье."""

import http.server
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-rel-")

from monitor import backup, collect, config, db, dumps, feeds, guard, health, log  # noqa: E402
from monitor.metrics import Meter  # noqa: E402


def fresh_db(name):
    d = os.path.join(config.DATA, "rel", name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return db.connect(os.path.join(d, "m.sqlite")), d


class FeedServer:
    """Локальный HTTP-сервер фида: отвечает по сценарию [(код, тело, заголовки), …], последний — дальше всегда."""

    def __init__(self, script):
        self.script = list(script)
        self.hits = 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                code, body, headers = outer.script[min(outer.hits - 1, len(outer.script) - 1)]
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/feed.yml"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


with open(os.path.join(FIX, "lemanapro_admitad.yml"), "rb") as _f:
    YML = _f.read()


class Log(unittest.TestCase):
    def test_json_lines_with_context(self):
        with log.context(run_id=77, site="lemanapro"):
            log.info("test.event", "Проверка", extra_field=5)
            log.debug("test.debug", "не видно на уровне INFO")
        rec = [r for r in log.tail(50, "DEBUG") if r["event"].startswith("test.")]
        self.assertEqual(len(rec), 1)
        r = rec[-1]
        self.assertEqual(
            (r["level"], r["run_id"], r["site"], r["extra_field"], r["msg"]), ("INFO", 77, "lemanapro", 5, "Проверка")
        )
        self.assertRegex(r["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertNotIn("run_id", log.current_context())

    def test_rotation(self):
        old = log.MAX_BYTES
        try:
            log.MAX_BYTES = 2000
            for i in range(60):
                log.info("rot.test", "x" * 50, i=i)
            self.assertTrue(os.path.exists(log.path() + ".1"))
            self.assertLessEqual(os.path.getsize(log.path()), 2000 + 400)
        finally:
            log.MAX_BYTES = old


class Retry(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        d = guard.backoff_delays(5, base=2, factor=4, max_delay=60)
        self.assertEqual(len(d), 4)
        self.assertTrue(1.6 <= d[0] <= 2.4 and 6.4 <= d[1] <= 9.6 and d[3] <= 72)

    def test_retry_then_success(self):
        calls, slept = [], []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("нет сети")
            return "ok"

        self.assertEqual(guard.retry(fn, attempts=3, base=1, sleep=slept.append, retry_on=(ConnectionError,)), "ok")
        self.assertEqual(len(slept), 2)

    def test_blocked_is_never_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise guard.Blocked("429")

        with self.assertRaises(guard.Blocked):
            guard.retry(fn, attempts=5, base=0, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)


class Breaker(unittest.TestCase):
    def test_errors_open_after_three_and_double(self):
        con, _ = fresh_db("breaker")
        now = 1_000_000.0
        self.assertIsNone(guard.failure(con, "s", "edge", "сбой", now))
        self.assertIsNone(guard.failure(con, "s", "edge", "сбой", now))
        until = guard.failure(con, "s", "edge", "сбой", now)
        self.assertEqual(until, now + 3600)
        self.assertEqual(guard.allow(con, "s", "edge", now + 100)[0], False)
        ok, b = guard.allow(con, "s", "edge", now + 3601)  # пауза прошла — пробная попытка
        self.assertTrue(ok)
        self.assertEqual(b["state"], "half")
        until2 = guard.failure(con, "s", "edge", "снова", now + 3700)  # проба не удалась — пауза вдвое
        self.assertEqual(until2, now + 3700 + 7200)
        guard.allow(con, "s", "edge", until2 + 1)
        guard.success(con, "s", "edge")
        st = guard.states(con)[0]
        self.assertEqual((st["state"], st["failures"], st["opens"]), ("closed", 0, 0))
        con.close()

    def test_block_pauses_at_least_an_hour_and_respects_retry_after(self):
        con, _ = fresh_db("block")
        now = 2_000_000.0
        self.assertEqual(guard.blocked(con, "s", "edge", "капча", None, now), now + 3600)
        guard.reset(con, "s", "edge")
        self.assertEqual(guard.blocked(con, "s", "feed", "429", 5 * 3600, now), now + 5 * 3600)
        guard.allow(con, "s", "feed", now + 5 * 3600 + 1)
        self.assertEqual(guard.blocked(con, "s", "feed", "429", None, now + 6 * 3600), now + 6 * 3600 + 7200)
        self.assertLessEqual(guard.MAX_PAUSE, 24 * 3600)
        con.close()

    def test_retry_after_parse(self):
        self.assertEqual(guard.parse_retry_after("120"), 120)
        self.assertIsNone(guard.parse_retry_after(""))
        self.assertGreater(guard.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 0)


class FeedNetwork(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(config.DATA, "rel", "feeds-" + self._testMethodName)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_retries_5xx_then_downloads(self):
        srv = FeedServer([(503, b"busy", None), (500, b"err", None), (200, YML, {"ETag": '"v1"'})])
        try:
            m = Meter("lemanapro", "feed")
            _path, meta = feeds.fetch(srv.url, self.dir, "lemanapro", meter=m, sleep=lambda s: None)
            self.assertEqual((meta["status"], srv.hits, m.retries, m.requests), ("скачан", 3, 2, 3))
            # 304 при повторе
            srv.script = [(304, b"", None)]
            srv.hits = 0
            _, meta = feeds.fetch(srv.url, self.dir, "lemanapro", force=True, sleep=lambda s: None)
            self.assertEqual(meta["status"], "не изменился")
        finally:
            srv.close()

    def test_429_raises_blocked_with_retry_after(self):
        srv = FeedServer([(429, b"slow down", {"Retry-After": "7200"})])
        try:
            m = Meter("lemanapro", "feed")
            with self.assertRaises(guard.Blocked) as cm:
                feeds.fetch(srv.url, self.dir, "lemanapro", meter=m, sleep=lambda s: None)
            self.assertEqual((cm.exception.reason, cm.exception.retry_after, srv.hits, m.http_429), ("429", 7200, 1, 1))
        finally:
            srv.close()

    def test_network_failure_falls_back_to_previous_file(self):
        srv = FeedServer([(200, YML, None)])
        feeds.fetch(srv.url, self.dir, "lemanapro", sleep=lambda s: None)
        srv.script = [(502, b"", None)]
        try:
            _, meta = feeds.fetch(srv.url, self.dir, "lemanapro", force=True, sleep=lambda s: None)
            self.assertTrue(meta["stale"])
            self.assertIn("взят прошлый файл", meta["status"])
            _, meta = feeds.fetch(srv.url, self.dir, "lemanapro", allow_network=False, force=True)
            self.assertIn("на паузе", meta["status"])
        finally:
            srv.close()

    def test_404_is_not_retried(self):
        srv = FeedServer([(404, b"", None)])
        try:
            with self.assertRaises(feeds.FeedError):
                feeds.fetch(srv.url, self.dir, "lemanapro", sleep=lambda s: None)
            self.assertEqual(srv.hits, 1)
        finally:
            srv.close()


def _settings(feed, report_dir):
    s = config.load()
    s["report_dir"] = report_dir
    s["sites"]["petrovich"]["enabled"] = False
    s["sites"]["lemanapro"].update(feed=feed, edge_enabled=False, feed_city="Москва")
    return s


class CollectReliability(unittest.TestCase):
    def test_429_pauses_feed_for_an_hour_and_uses_previous_file(self):
        con, d = fresh_db("c429")
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        srv = FeedServer([(200, YML, None)])
        try:
            s = _settings(srv.url, os.path.join(d, "reports"))
            r1 = collect.run(s, con=con)
            self.assertEqual(r1["prices"], 7)
            srv.script, srv.hits = [(429, b"", {"Retry-After": "60"})], 0
            r2 = collect.run(s, con=con, force_feed=True)
            self.assertEqual(r2["prices"], 7)  # работали с прошлым файлом
            b = {(x["site"], x["source"]): x for x in guard.states(con)}[("lemanapro", "feed")]
            self.assertEqual(b["state"], "open")
            self.assertGreaterEqual(b["until"] - time.time(), 3500)  # не меньше часа, хоть Retry-After = 60
            hits = srv.hits
            collect.run(s, con=con, force_feed=True)
            self.assertEqual(srv.hits, hits)  # на паузе — к серверу не ходили
            m = next(x for x in db.metrics_recent(con, 5) if x["run_id"] == r2["run_id"])
            self.assertEqual((m["http_429"], m["outcome"]), (1, "blocked"))
            self.assertTrue(any("429" in w for w in r2["warnings"]))
        finally:
            srv.close()
            con.close()

    def test_single_run_lock(self):
        con, d = fresh_db("lock")
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        s = _settings(os.path.join(FIX, "lemanapro_admitad.yml"), os.path.join(d, "reports"))
        self.assertIsNone(db.acquire_lock(con, collect.LOCK, "другой-процесс"))
        r = collect.run(s, con=con)
        self.assertTrue(r.get("skipped"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        db.release_lock(con, collect.LOCK, "другой-процесс")
        # зависшая блокировка (нет сердцебиения 15 мин) не мешает
        db.acquire_lock(con, collect.LOCK, "умерший")
        con.execute("UPDATE locks SET heartbeat=heartbeat-3600")
        r = collect.run(s, con=con)
        self.assertFalse(r.get("skipped"))
        self.assertIsNone(db.lock_info(con, collect.LOCK))  # после сбора блокировка снята
        con.close()

    def test_interrupted_runs_are_marked(self):
        con, d = fresh_db("interrupted")
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        rid = db.start_run(con)  # «выключили компьютер» посреди сбора
        r = collect.run(_settings(os.path.join(FIX, "lemanapro_admitad.yml"), os.path.join(d, "r")), con=con)
        st = dict(con.execute("SELECT id, status FROM runs").fetchall())
        self.assertEqual((st[rid], st[r["run_id"]]), ("прерван", "готово"))
        con.close()

    def test_write_is_atomic_and_idempotent(self):
        con, _d = fresh_db("atomic")
        item = {"key": "k1", "site": "lemanapro", "price": 100.0, "source": "feed", "name": "Товар"}
        with db.transaction(con):
            db.record(con, 5, item, ts=1.0)
        db.record(con, 5, dict(item, price=110.0), ts=1.0)  # повтор записи того же сбора
        self.assertEqual(con.execute("SELECT COUNT(*) FROM history").fetchone()[0], 1)
        with self.assertRaises(RuntimeError), db.transaction(con):
            db.record(con, 6, dict(item, key="k2"), ts=2.0)
            raise RuntimeError("сбой посреди записи")
        self.assertIsNone(con.execute("SELECT 1 FROM products WHERE key='k2'").fetchone())
        con.close()

    def test_metrics_saved_per_source(self):
        con, d = fresh_db("metrics")
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        r = collect.run(_settings(os.path.join(FIX, "lemanapro_admitad.yml"), os.path.join(d, "r")), con=con)
        rows = db.metrics_recent(con, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["site"], rows[0]["source"], rows[0]["prices"], rows[0]["outcome"]), ("lemanapro", "feed", 7, "ok")
        )
        self.assertEqual(r["metrics"][0]["prices"], 7)
        con.close()


class Migrations(unittest.TestCase):
    def test_upgrade_from_2_2_schema(self):
        d = os.path.join(config.DATA, "rel", "mig")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        p = os.path.join(d, "old.sqlite")
        old = sqlite3.connect(p)
        old.executescript("""CREATE TABLE history(id INTEGER PRIMARY KEY, key TEXT NOT NULL, ts REAL NOT NULL,
            price REAL, old_price REAL, available INTEGER, source TEXT NOT NULL, city TEXT);
            INSERT INTO history(key, ts, price, source) VALUES('a', 1, 10, 'feed');""")
        old.commit()
        old.close()
        con = db.connect(p)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        cols = [r[1] for r in con.execute("PRAGMA table_info(history)")]
        self.assertIn("run_id", cols)
        self.assertEqual(con.execute("SELECT price FROM history").fetchone()[0], 10)  # данные целы
        con.close()
        con = db.connect(p)  # повторное открытие — без ошибок
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        con.close()


class Backups(unittest.TestCase):
    def test_make_prune_restore(self):
        con, d = fresh_db("backup")
        con.execute("INSERT INTO decisions VALUES('x','exclude','',1)")
        shutil.rmtree(backup.folder(), ignore_errors=True)
        path = backup.make(con, "тест")
        self.assertTrue(path and os.path.exists(path))
        self.assertFalse(backup.due(20))
        con.execute("DELETE FROM decisions")
        db_path = os.path.join(d, "m.sqlite")
        con.close()
        backup.restore(path, db_path)
        con = db.connect(db_path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
        self.assertTrue(any(n.startswith("before-restore-") for n in os.listdir(backup.folder())))
        # хранение: 10 последних + по одной за неделю
        for i in range(14):
            fake = os.path.join(backup.folder(), f"{backup.PREFIX}2026010{i % 10}-{i:06d}.zip")
            shutil.copy(path, fake)
            ts = time.time() - (i + 1) * 3 * 86400
            os.utime(fake, (ts, ts))
        backup.prune()
        self.assertLessEqual(len(backup.items()), backup.KEEP_LAST + backup.KEEP_WEEKS)
        self.assertEqual(backup.items()[0]["path"], path)  # самая новая на месте
        con.close()

    def test_settings_backup_on_save(self):
        s = config.load()
        config.save(s)
        s["brand_words"] = ["Основит", "Тест"]
        config.save(s)
        with open(config.SETTINGS_PATH + ".bak", encoding="utf-8") as f:
            self.assertNotIn("Тест", f.read())


class Dumps(unittest.TestCase):
    def test_save_and_prune(self):
        shutil.rmtree(dumps.folder(), ignore_errors=True)
        with log.context(run_id=9):
            p = dumps.save(
                "lemanapro",
                "captcha",
                url="https://lemanapro.ru/x/",
                status=403,
                html="<html>капча</html>",
                screenshot_b64="aGVsbG8=",
            )
        with open(os.path.join(p, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(
            (meta["run_id"], meta["status"], sorted(meta["files"])), (9, 403, ["page.html.gz", "screen.jpg"])
        )
        for _i in range(5):
            dumps.save("lemanapro", "empty", html="x")
        self.assertEqual(dumps.prune(max_items=3), 3)
        self.assertEqual(len(os.listdir(dumps.folder())), 3)


class Health(unittest.TestCase):
    def test_dead_mans_switch(self):
        con, d = fresh_db("health")
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        s = _settings(os.path.join(FIX, "lemanapro_admitad.yml"), os.path.join(d, "r"))
        collect.run(s, con=con)
        now = time.time()
        self.assertNotEqual(health.check(con, s, now)["status"], "fail")
        sent = []
        hook = FeedServer([(200, b"ok", None)])
        s["alerts"] = {"windows": False, "webhook_url": hook.url.replace("feed.yml", "send?text={text}")}
        state = os.path.join(config.DATA, health.STATE_FILE)
        if os.path.exists(state):
            os.remove(state)
        try:
            later = now + 30 * 3600  # сбор не проходил 30 часов
            h = health.check(con, s, later)
            self.assertEqual(h["status"], "fail")
            self.assertIn("Сбор", [c["name"] for c in h["checks"] if c["status"] == "fail"])
            r1 = health.watchdog(con, s, later)
            r2 = health.watchdog(con, s, later + 3600)  # та же тревога через час — не повторяем
            r3 = health.watchdog(con, s, later + 25 * 3600)  # через сутки — напоминаем
            sent = [r1["alerted"], r2["alerted"], r3["alerted"]]
            self.assertEqual(sent, [True, False, True])
            self.assertEqual(r1["channels"], ["webhook"])
            self.assertEqual(hook.hits, 2)
            r4 = health.watchdog(con, s, now + 60)  # всё снова хорошо — одно сообщение
            self.assertTrue(r4["alerted"])
            self.assertFalse(health.watchdog(con, s, now + 120)["alerted"])
        finally:
            hook.close()
            con.close()

    def test_open_breaker_is_a_warning(self):
        con, _d = fresh_db("health2")
        guard.blocked(con, "lemanapro", "edge", "капча")
        h = health.check(con, config.load())
        self.assertTrue(any("пауз" in c["detail"] for c in h["checks"] if c["status"] == "warn"))
        con.close()


if __name__ == "__main__":
    unittest.main()
