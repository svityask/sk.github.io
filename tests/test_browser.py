"""Окно сбора в настоящем браузере на поддельном сайте (tests/fakesite.py).

Запускается, если найден Chromium/Chrome/Edge (или задан OSNOVIT_BROWSER). В CI — на Ubuntu с Google Chrome.
Паузы между страницами в тесте отключены, остальное — как в жизни: robots.txt, листание раздела, данные страницы,
разметка, 429, капча, город, дампы при сбоях, сверка фида с полкой.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-br-")

from fakesite import FakeSite  # noqa: E402

from monitor import cdp, collect, config, db, dumps, guard  # noqa: E402
from monitor.visitor import Stop, Visitor  # noqa: E402

BROWSER = cdp.find_browser(os.environ.get("OSNOVIT_BROWSER", ""))
FIX = os.path.join(ROOT, "tests", "fixtures")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(
    BROWSER and not os.environ.get("OSNOVIT_SKIP_BROWSER"), "нет браузера (Chromium/Chrome/Edge) — тест окна пропущен"
)
class BrowserWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FakeSite()
        cls.port = free_port()
        cls.profile = tempfile.mkdtemp(prefix="osnovit-profile-")
        args = [
            BROWSER,
            "--headless=new",
            "--no-sandbox",
            f"--remote-debugging-port={cls.port}",
            f"--user-data-dir={cls.profile}",
            "--no-first-run",
            *cls.site.browser_args(),
            "about:blank",
        ]
        cls.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if cdp.debugger_alive(cls.port):
                break
            time.sleep(0.5)
        else:
            raise unittest.SkipTest("браузер не запустился")
        cls._pause = Visitor._pause
        Visitor._pause = lambda self: None  # в тесте не ждём 5–15 с между страницами

    @classmethod
    def tearDownClass(cls):
        Visitor._pause = cls._pause
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.site.close()
        shutil.rmtree(cls.profile, ignore_errors=True)

    def setUp(self):
        self.site.log.clear()
        self.site.city = "Москва"

    def settings(self):
        s = config.load()
        s["edge"].update(port=self.port, wait_check_s=20, max_pages=40)
        return s

    def visitor(self, interactive=False):
        v = Visitor(self.settings(), collect.Status(), interactive=interactive)
        v.open()  # браузер уже слушает порт — подключаемся к нему
        self.addCleanup(v.close)
        return v

    def test_section_pagination_robots_and_data(self):
        v = self.visitor()
        res = v.run_site(
            "lemanapro",
            [
                (1, "https://lemanapro.ru/catalogue/shtukaturki/", "Штукатурки"),
                (2, "https://lemanapro.ru/catalogue/zapret/", "Запрет"),
            ],
            [(1, "https://lemanapro.ru/product/shpaklevka-osnovit-66666666/", "66666666")],
            expect_city="Москва",
        )
        self.assertIsNone(res["stopped"])
        by = {i["code"]: i for i in res["items"]}
        self.assertEqual(by["82065432"]["price"], 585)  # из данных страницы, не цена по карте
        self.assertEqual(by["82065432"]["old_price"], 640)
        self.assertEqual(by["12345690"]["price"], 505)  # вторая страница выдачи
        self.assertEqual(by["12345680"]["price"], 455)  # из вёрстки, без цены по карте
        self.assertEqual(by["66666666"]["via"], "разметка")
        self.assertNotIn("/catalogue/zapret/", self.site.log)  # robots.txt соблюдён
        self.assertIn("/catalogue/shtukaturki/?page=2", self.site.log)
        self.assertEqual(res["city"], "Москва")

    def test_check_page_that_clears_itself(self):
        v = self.visitor()
        res = v.run_site("lemanapro", [(3, "https://lemanapro.ru/catalogue/check/", "Проверка")], [])
        self.assertEqual([(i["code"], i["price"]) for i in res["items"]], [("12345699", 333)])
        self.assertEqual(v.meter.captcha, 1)

    def test_429_blocks_and_dumps(self):
        shutil.rmtree(dumps.folder(), ignore_errors=True)
        v = self.visitor()
        res = v.run_site("lemanapro", [(1, "https://lemanapro.ru/catalogue/limit/", "Лимит")], [])
        self.assertEqual((res["blocked"]["reason"], res["blocked"]["retry_after"]), ("429", 5400))
        self.assertEqual(v.meter.http_429, 1)
        d = dumps.recent(1)[0]
        self.assertEqual(d["reason"], "429")
        self.assertIn("screen.jpg", d["files"])
        self.assertIn("page.html.gz", d["files"])

    def test_captcha_without_human_blocks(self):
        v = self.visitor(interactive=False)
        res = v.run_site("lemanapro", [(1, "https://lemanapro.ru/catalogue/captcha/", "Капча")], [])
        self.assertEqual(res["blocked"]["reason"], "denied")
        self.assertTrue(res["stopped"])
        self.assertEqual(v.meter.captcha, 1)

    def test_other_city_stops_without_writing(self):
        self.site.city = "Санкт-Петербург"
        v = self.visitor()
        res = v.run_site(
            "lemanapro", [(1, "https://lemanapro.ru/catalogue/shtukaturki/", "Штукатурки")], [], expect_city="Москва"
        )
        self.assertIn("Санкт-Петербург", res["stopped"])
        self.assertEqual(res["items"], [])

    def test_full_run_with_crosscheck_and_pause(self):
        d = os.path.join(config.DATA, "browser-run")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        con = db.connect(os.path.join(d, "m.sqlite"))
        s = self.settings()
        s["report_dir"] = os.path.join(d, "reports")
        s["crosscheck"]["n"] = 2
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(
            feed=os.path.join(FIX, "lemanapro_admitad.yml"), feed_city="Москва", edge_city="Москва", edge_enabled=True
        )
        db.add_tracked(con, "lemanapro", "category", "110", "Сухие смеси")
        db.add_tracked(con, "lemanapro", "section", "https://lemanapro.ru/catalogue/shtukaturki/", "Штукатурки")
        r = collect.run(s, con=con, interactive=False)
        self.assertNotIn("error", r, r.get("trace"))
        cc = r["sites"]["lemanapro"]["crosscheck"]
        self.assertGreaterEqual(cc["n"], 3)  # 82065432, 12345678 из раздела + карточка
        self.assertIn("lemanapro:66666666", {i["key"] for i in cc["items"]})
        self.assertIn("lemanapro:12345690", {p["key"] for p in db.products_of_run(con, r["run_id"])})
        edge = next(m for m in r["metrics"] if m["source"] == "edge")
        self.assertEqual(edge["outcome"], "partial")  # карточки без цены на поддельном сайте
        # капча без человека — сеть на паузе, следующий сбор её пропускает
        db.add_tracked(con, "lemanapro", "section", "https://lemanapro.ru/catalogue/captcha/", "А-капча")
        r2 = collect.run(s, con=con, interactive=False)
        self.assertTrue(any("на паузе" in w for w in r2["warnings"]))
        n = len(self.site.log)
        r3 = collect.run(s, con=con, interactive=False)
        self.assertEqual(len(self.site.log), n)  # на паузе к сайту не ходили
        self.assertEqual([m["outcome"] for m in r3["metrics"] if m["source"] == "edge"], ["skipped"])
        b = next(x for x in guard.states(con) if x["source"] == "edge")
        self.assertGreaterEqual(b["until"] - time.time(), 3500)
        con.close()

    # ---------------------------------------------------------------- листание выдачи

    def test_pages_by_numbers_and_dalshe(self):
        """Петрович: «1 2 3 … Дальше» без rel="next" — листаем до конца раздела, характеристики берём из списка."""
        v = self.visitor()
        res = v.run_site("petrovich", [(1, "https://petrovich.ru/catalog/shtukaturki/", "Штукатурки")], [])
        self.assertIsNone(res["stopped"])
        self.assertEqual(len(res["items"]), 6)  # 3 страницы × 2 товара
        self.assertIn("/catalog/shtukaturki/?p=3", self.site.log)
        self.assertNotIn("/catalog/shtukaturki/?tags=1", self.site.log)  # «Показать все…» — не следующая страница
        it = res["items"][0]
        self.assertEqual(it["params"].get("Вес, кг"), ("30", ""))  # из списка, без захода в карточку
        self.assertEqual(it["params"].get("Основа"), ("Гипсовая", ""))

    def test_show_more_button(self):
        """Лемана: кнопка «Показать ещё» без ссылки — нажимаем, пока товары прибавляются."""
        v = self.visitor()
        res = v.run_site("lemanapro", [(1, "https://lemanapro.ru/catalogue/pokazat/", "Затирки")], [])
        self.assertIsNone(res["stopped"])
        self.assertEqual(len(res["items"]), 6)  # 2 + 2 нажатия по 2
        self.assertEqual(res["pages"], 4)  # robots.txt + страница + 2 нажатия: нажатие считается как страница

    def test_late_and_lazy_items(self):
        """Товары дорисовываются через 3 с и догружаются прокруткой — не считаем страницу пустой."""
        v = self.visitor()
        res = v.run_site("lemanapro", [(1, "https://lemanapro.ru/catalogue/lenivo/", "Клеи")], [])
        codes = {i["code"] for i in res["items"]}
        self.assertTrue({"66600001", "66600002"} <= codes)
        self.assertEqual(v.meter.empty, 0)

    # ---------------------------------------------------------------- стратегия «полка → карточки»

    FEED = """<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="2026-10-06 06:00"><shop><name>Лемана ПРО</name>
<categories><category id="1">Смеси</category></categories><offers>
<offer id="11111111" available="true"><url>https://lemanapro.ru/product/kley-osnovit-pliteks-11111111/</url>
<price>400</price><currencyId>RUR</currencyId><categoryId>1</categoryId>
<name>Клей плиточный Основит Плитэкс C1 25 кг</name><vendor>Основит</vendor></offer>
<offer id="22222222" available="true"><url>https://lemanapro.ru/product/kley-cerezit-cm11-22222222/</url>
<price>450</price><currencyId>RUR</currencyId><categoryId>1</categoryId>
<name>Клей плиточный Церезит CM11 25 кг</name><vendor>Церезит</vendor></offer>
<offer id="82065432" available="true"><url>https://lemanapro.ru/product/shtukaturka-osnovit-82065432/</url>
<price>590</price><currencyId>RUR</currencyId><categoryId>1</categoryId>
<name>Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг</name><vendor>Основит</vendor></offer>
</offers></shop></yml_catalog>"""

    def strategy_run(self, feed_city):
        d = os.path.join(config.DATA, "strategy-" + feed_city)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        feed = os.path.join(d, "feed.yml")
        with open(feed, "w", encoding="utf-8") as f:
            f.write(self.FEED)
        con = db.connect(os.path.join(d, "m.sqlite"))
        self.addCleanup(con.close)
        s = self.settings()
        s["report_dir"] = os.path.join(d, "reports")
        s["crosscheck"]["n"] = 0  # случайную сверку выключаем: проверяем только решения стратегии
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(
            feed=feed, feed_city=feed_city, edge_city="", edge_enabled=True, edge_prepare=True
        )
        db.add_tracked(con, "lemanapro", "category", "1", "Смеси")
        db.add_tracked(con, "lemanapro", "section", "https://lemanapro.ru/catalogue/smesi/", "Смеси")
        status = collect.Status()
        asked = []
        stop = threading.Event()

        def human():  # «человек»: выбрал город и жмёт «Готово» на каждую новую просьбу приложения
            seen = None
            while not stop.is_set():
                msg = status.human
                if msg and msg is not seen:  # новая просьба может прийти сразу за прошлой — сравниваем сам объект
                    seen = msg
                    asked.append(msg)
                    status.human_done()
                time.sleep(0.05)

        t = threading.Thread(target=human, daemon=True)
        t.start()
        try:
            r = collect.run(s, status, con=con, interactive=True)
        finally:
            stop.set()
            t.join(5)
        self.assertNotIn("error", r, r.get("trace"))
        return r, con, asked

    def test_shallow_match_deep(self):
        r, con, asked = self.strategy_run("Москва")
        self.assertEqual(len(asked), 1)
        self.assertIn("Выберите в нём город", asked[0])
        log = self.site.log
        self.assertIn("/", log)  # окно открыли на главной — для человека
        self.assertIn("/catalogue/smesi/", log)
        # deep — только где нужно
        self.assertIn("/product/kley-cerezit-cm11-22222222/", log)  # 520 на полке против 450 в фиде
        self.assertIn("/product/kley-volma-keramik-33333333/", log)  # «от 300 ₽» и нет фасовки
        self.assertIn("/product/grunt-unis-44444444/", log)  # нет фасовки
        self.assertNotIn("/product/kley-osnovit-pliteks-11111111/", log)  # цена совпала с фидом
        self.assertNotIn("/product/shtukaturka-osnovit-bez-koda/", log)  # сопоставлен по названию, цена совпала
        st = r["sites"]["lemanapro"]["edge"]["strategy"]
        self.assertEqual(st["shallow"], 5)
        self.assertEqual(st["matched"], {"артикул": 2, "название": 1})
        self.assertEqual(st["deep_pages"], 3)
        self.assertEqual(st["deep"], {"расходится с фидом": 1, "цена от": 1, "фасовка": 2})
        prods = {p["key"]: p for p in db.products_of_run(con, r["run_id"])}
        volma = prods["lemanapro:33333333"]
        self.assertEqual((volma["price"], volma["pack_qty"], volma["pack_unit"]), (310, 25, "кг"))  # с карточки
        grunt = prods["lemanapro:44444444"]
        self.assertEqual((grunt["pack_qty"], grunt["pack_unit"]), (10, "л"))  # фасовка из характеристик
        self.assertNotIn("lemanapro:shtukaturka-osnovit-bez-koda", prods)  # не задвоился — это товар фида
        gaps = {i["key"] for i in r["review_items"] if i["kind"] == "feedgap"}
        self.assertEqual(gaps, {"lemanapro:22222222"})  # расхождение подтвердила карточка
        self.assertEqual(config.load()["sites"]["lemanapro"]["edge_city"], "Москва")  # город запомнен

    def test_other_city_in_window_is_not_mixed(self):
        n = len(self.site.log)
        r, _con, asked = self.strategy_run("Санкт-Петербург")
        self.assertEqual(len(asked), 2)  # второй раз попросили выбрать город фида
        self.assertIn("Санкт-Петербург", asked[1])
        self.assertTrue(any("не смешать цены" in w for w in r["warnings"]))
        self.assertNotIn("/catalogue/smesi/", self.site.log[n:])  # с сайта ничего не собирали


class StopIsAnException(unittest.TestCase):
    def test_stop_flags(self):
        self.assertTrue(Stop("x", failed=True).failed)
        self.assertFalse(Stop("x").failed)


if __name__ == "__main__":
    unittest.main()
