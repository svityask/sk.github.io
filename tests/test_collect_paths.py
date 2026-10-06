"""Исправления 2.4.3: фид (ссылки партнёрских сетей, страница вместо фида), окно Edge, сохранение отчёта Excel."""

import http.server
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-paths-")

from monitor import cdp, collect, config, db, feeds, report, sites, xlsx  # noqa: E402
from monitor.visitor import Visitor  # noqa: E402


def tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    return d


class AffiliateLinks(unittest.TestCase):
    def test_known_and_unknown_wrappers(self):
        cases = {
            # Admitad
            "https://ad.admitad.com/g/abc/?i=5&ulp=https%3A%2F%2Flemanapro.ru%2Fproduct%2Fx-82065432%2F": "82065432",
            # «Где Слон?» — параметр goto
            "https://f.gdeslon.ru/cf/abc?mid=1&goto=https%3A%2F%2Fpetrovich.ru%2Fproduct%2F101201%2F": "101201",
            # незнакомая сеть, незнакомый параметр, адрес закодирован дважды
            "https://t.example/r?q=1&weird=https%253A%252F%252Fpetrovich.ru%252Fproduct%252F555555%252F": "555555",
            # обёртка в обёртке
            "https://a.example/x?next=https%3A%2F%2Fb.example%2Fy%3Fu%3Dhttps%253A%252F%252Flemanapro.ru"
            "%252Fproduct%252Fa-12345678%252F": "12345678",
        }
        for url, code in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sites.code_from_url(url), code)
        self.assertEqual(
            sites.unwrap_link("https://other.ru/product/1/?u=https%3A%2F%2Fother2.ru"),
            "https://other.ru/product/1/?u=https%3A%2F%2Fother2.ru",
        )

    def test_tracker_without_site_address_is_not_a_product_url(self):
        """Ссылка-счётчик без адреса товара внутри не становится адресом товара (её нельзя открывать в окне)."""
        d = tmpdir("osnovit-trk-")
        p = os.path.join(d, "feed.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("id;name;price;url\n")
            f.write("1;Штукатурка гипсовая 30 кг;500;https://track.example/click/abc123\n")
            f.write(
                "2;Шпаклёвка 20 кг;400;https://ad.admitad.com/g/x/?ulp=https%3A%2F%2Fpetrovich.ru%2Fproduct%2F222222%2F\n"
            )
        feed = feeds.read(p, "petrovich")
        by = {o["offer_id"]: o for o in feed.offers}
        self.assertEqual(by["1"]["url"], "")
        self.assertEqual(by["1"]["key"], "petrovich:1")  # ключ — по id из фида
        self.assertEqual(by["2"]["url"], "https://petrovich.ru/product/222222/")
        self.assertEqual(feed.tracker_links, 1)
        self.assertNotIn("_tracker", by["1"])


class FeedServer:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = outer.bodies.pop(0) if len(outer.bodies) > 1 else outer.bodies[0]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/feed.yml"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NotAFeed(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIX, "lemanapro_admitad.yml"), "rb") as f:
            self.good = f.read()
        self.login = b"<!DOCTYPE html><html><head><title>Admitad</title></head><body>" + b"x" * 500 + b"</body></html>"

    def test_login_page_keeps_last_good_feed(self):
        srv = FeedServer([self.good, self.login])
        self.addCleanup(srv.close)
        d = tmpdir("osnovit-naf-")
        path, meta = feeds.fetch(srv.url, d, "lemanapro", force=True)
        self.assertEqual(meta["status"], "скачан")
        path, meta = feeds.fetch(srv.url, d, "lemanapro", force=True)
        self.assertTrue(meta["stale"])
        self.assertIn("веб-страницу", meta["status"])
        self.assertGreater(feeds.read(path, "lemanapro").total, 0)  # с ним сбор пройдёт
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.good)  # хороший файл не затёрт
        self.assertFalse(os.path.exists(path + ".part"))

    def test_login_page_without_previous_file_is_an_error(self):
        srv = FeedServer([self.login])
        self.addCleanup(srv.close)
        with self.assertRaises(feeds.FeedError) as cm:
            feeds.fetch(srv.url, tmpdir("osnovit-naf-"), "lemanapro", force=True)
        self.assertIn("веб-страницу", str(cm.exception))

    def test_json_error(self):
        srv = FeedServer([b'{"error": "invalid token", "details": "' + b"x" * 100 + b'"}'])
        self.addCleanup(srv.close)
        with self.assertRaises(feeds.FeedError) as cm:
            feeds.fetch(srv.url, tmpdir("osnovit-naf-"), "lemanapro", force=True)
        self.assertIn("JSON", str(cm.exception))

    def test_local_file_clears_old_failure(self):
        d = tmpdir("osnovit-naf-")
        feeds._save_json(os.path.join(d, "lemanapro.feed.json"), {"stale": True, "status": "не скачался"})
        _path, meta = feeds.fetch(os.path.join(FIX, "lemanapro_admitad.yml"), d, "lemanapro")
        self.assertFalse(meta["stale"])


class FakeTab:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, method, timeout=60, **params):
        self.sent.append(method)
        return {}

    def close(self):
        self.closed = True


class EdgeWindow(unittest.TestCase):
    def visitor(self):
        return Visitor(config.load(), collect.Status(), interactive=False)

    def test_cards_without_code_are_all_opened_once(self):
        """Товар без артикула на странице не «закрывает» остальные карточки без артикула."""
        v = self.visitor()
        opened = []

        def card(site, url, code, expect_city, res, status_text):
            opened.append(url)
            return {"code": None, "url": url, "price": 100.0, "via": "разметка"}

        products = [
            (1, "https://lemanapro.ru/product/a-11111111/", None),
            (1, "https://lemanapro.ru/product/b-22222222/", None),
            (1, "https://lemanapro.ru/product/a-11111111/", None),  # тот же ещё раз — не открываем
        ]
        with mock.patch.object(v, "_card", side_effect=card):
            res = v.run_site("lemanapro", [], products)
        self.assertEqual(
            opened, ["https://lemanapro.ru/product/a-11111111/", "https://lemanapro.ru/product/b-22222222/"]
        )
        self.assertEqual(len(res["items"]), 2)

    def test_check_skips_card_already_opened(self):
        v = self.visitor()
        opened = []

        def card(site, url, code, expect_city, res, status_text):
            opened.append(url)
            return {"code": code, "url": url, "price": 100.0, "via": "разметка"}

        url = "https://lemanapro.ru/product/a-11111111/"
        with mock.patch.object(v, "_card", side_effect=card):
            v.run_site(
                "lemanapro", [], [(1, url, "11111111")], checks=[("lemanapro:11111111", url, "11111111")], checks_n=3
            )
        self.assertEqual(opened, [url])

    def test_close_shuts_window_we_started(self):
        v = self.visitor()
        tab = FakeTab()
        proc = mock.Mock()
        v._tab, v.proc = tab, proc
        v.close()
        self.assertEqual(tab.sent, ["Browser.close"])
        self.assertTrue(tab.closed)
        proc.wait.assert_called()
        self.assertIsNone(v.proc)
        self.assertFalse(v.is_open)

    def test_close_keeps_window_someone_else_opened(self):
        v = self.visitor()
        tab = FakeTab()
        v._tab, v.proc = tab, None  # окно уже было открыто (например, человек выбирал город)
        v.close()
        self.assertEqual(tab.sent, [])
        self.assertTrue(tab.closed)

    def test_close_survives_dead_browser(self):
        v = self.visitor()
        tab = FakeTab()
        tab.send = mock.Mock(side_effect=cdp.BrowserError("Окно браузера закрыто"))
        v._tab, v.proc = tab, mock.Mock()
        v.close()  # не падает
        self.assertTrue(tab.closed)


class PrepareBlocked(unittest.TestCase):
    def test_429_while_preparing_pauses_the_source(self):
        """429 ещё при подготовке окна — источник на паузе, как при сборе, а не «ошибка сети»."""
        from monitor import guard

        data = tmpdir("osnovit-pb-")
        con = db.connect(os.path.join(data, "m.sqlite"))
        self.addCleanup(con.close)
        s = config.load()
        s["report_dir"] = os.path.join(data, "reports")
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(feed="", edge_enabled=True, edge_prepare=True)
        db.add_tracked(con, "lemanapro", "section", "https://lemanapro.ru/catalogue/x/", "Раздел")
        with (
            mock.patch.object(Visitor, "open"),
            mock.patch.object(Visitor, "close"),
            mock.patch.object(Visitor, "prepare", side_effect=guard.Blocked("429", 7200, "сайт ответил 429")),
            mock.patch.object(Visitor, "run_site") as run_site,
        ):
            r = collect.run(s, collect.Status(), con=con, interactive=True)
        run_site.assert_not_called()
        self.assertNotIn("error", r)
        self.assertNotIn("error", r["sites"]["lemanapro"])  # не «сбой сети»
        self.assertIn("на паузе", r["sites"]["lemanapro"]["edge"]["error"])
        b = next(x for x in guard.states(con) if x["source"] == "edge")
        self.assertEqual(b["state"], "open")


class CancelIsNotDone(unittest.TestCase):
    def test_cancel_while_waiting_for_human(self):
        st = collect.Status()
        threading.Timer(0.2, st.cancel).start()
        self.assertFalse(st.ask_human("Пройдите проверку", lambda: False, 30))
        st = collect.Status()
        threading.Timer(0.2, st.human_done).start()
        self.assertTrue(st.ask_human("Пройдите проверку", lambda: False, 30))

    def test_cancel_during_prepare_is_a_stop_not_a_city(self):
        v = Visitor(config.load(), collect.Status(), interactive=True)
        v._tab = mock.Mock()
        v.allowed = mock.Mock(return_value=False)
        v.status.cancel()
        from monitor.visitor import Stop

        with self.assertRaises(Stop) as cm:
            v.prepare("lemanapro")
        self.assertIn("остановлен", str(cm.exception))
        v._tab.evaluate.assert_not_called()  # город с недоготовленной страницы не читали


class ReportFile(unittest.TestCase):
    def sheets(self):
        sh = xlsx.Sheet("Лист", [("A", 10, xlsx.TEXT)])
        sh.add("x")
        return [sh]

    def test_same_minute_gets_new_name_and_no_temp_left(self):
        d = tmpdir("osnovit-rep-")
        s = {"report_dir": d}
        p1 = report.save(self.sheets(), s, {})
        p2 = report.save(self.sheets(), s, {})
        p3 = report.save(self.sheets(), s, {})
        self.assertEqual(len({p1, p2, p3}), 3)
        self.assertTrue(p2.endswith(" (2).xlsx"))
        self.assertTrue(p3.endswith(" (3).xlsx"))
        self.assertEqual(sorted(os.listdir(d)), sorted(os.path.basename(p) for p in (p1, p2, p3)))

    def test_failed_write_leaves_nothing(self):
        d = tmpdir("osnovit-rep-")
        with mock.patch.object(report, "write", side_effect=OSError("нет места")), self.assertRaises(OSError):
            report.save(self.sheets(), {"report_dir": d}, {})
        self.assertEqual(os.listdir(d), [])

    def test_unavailable_folder_falls_back_with_warning(self):
        d = tmpdir("osnovit-rep-")
        blocker = os.path.join(d, "файл")
        with open(blocker, "w") as f:
            f.write("x")
        home = tmpdir("osnovit-home-")
        summary: dict = {}
        with mock.patch.object(config, "default_report_dir", return_value=os.path.join(home, "Отчёты")):
            p = report.save(self.sheets(), {"report_dir": os.path.join(blocker, "отчёты")}, summary)
        self.assertTrue(p.startswith(os.path.join(home, "Отчёты")))
        self.assertIn("Папка отчётов недоступна", summary["warnings"][0])


class ReportFailureIsNotRunFailure(unittest.TestCase):
    def test_prices_saved_without_report(self):
        data = tmpdir("osnovit-rf-")
        feed = os.path.join(data, "lm.yml")
        shutil.copy(os.path.join(FIX, "lemanapro_admitad.yml"), feed)
        con = db.connect(os.path.join(data, "m.sqlite"))
        self.addCleanup(con.close)
        s = config.load()
        s["report_dir"] = os.path.join(data, "reports")
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(feed=feed, feed_city="Москва", edge_enabled=False)
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        with mock.patch.object(report, "build", side_effect=PermissionError(13, "файл открыт в Excel")):
            r = collect.run(s, con=con)
        self.assertNotIn("error", r)
        self.assertIsNone(r["report"])
        self.assertTrue(any("Отчёт Excel не сохранился" in w for w in r["warnings"]))
        status = con.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(status, "готово")
        self.assertGreater(con.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
