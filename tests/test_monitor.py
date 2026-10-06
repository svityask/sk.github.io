"""Офлайн-тесты: фиды, фасовка, ссылки, разбор страниц, анализ, отчёт. Запуск: python -m unittest discover tests"""

import gzip
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")

TMP = tempfile.mkdtemp(prefix="osnovit-test-")
os.environ["OSNOVIT_DIY_DATA"] = TMP

from monitor import analysis, collect, config, db, extract, feeds, kinds, sites, units, xlsx  # noqa: E402


class Sites(unittest.TestCase):
    def test_unwrap_admitad(self):
        u = "https://ad.admitad.com/g/abc/?i=5&ulp=https%3A%2F%2Flemanapro.ru%2Fproduct%2Fx-82065432%2F%3Futm%3D1"
        self.assertEqual(sites.normalize_url(u), "https://lemanapro.ru/product/x-82065432/")

    def test_leroy_to_lemana(self):
        self.assertEqual(
            sites.normalize_url("http://www.leroymerlin.ru/product/klej-12345678"),
            "https://lemanapro.ru/product/klej-12345678/",
        )

    def test_codes(self):
        self.assertEqual(sites.code_from_url("https://petrovich.ru/product/101201/"), "101201")
        self.assertEqual(sites.code_from_url("https://moscow.petrovich.ru/catalog/1546/101202/"), "101202")
        self.assertEqual(sites.code_from_url("https://lemanapro.ru/product/shtukaturka-30-kg-82065432/"), "82065432")
        self.assertIsNone(sites.code_from_url("https://lemanapro.ru/catalogue/shtukaturki/"))

    def test_kinds_and_city(self):
        self.assertTrue(sites.is_section_url("https://lemanapro.ru/catalogue/shtukaturki/"))
        self.assertTrue(sites.is_section_url("https://petrovich.ru/catalog/1546/"))
        self.assertEqual(sites.city_from_url("https://moscow.petrovich.ru/product/1/"), "Москва")
        self.assertEqual(sites.city_from_url("https://petrovich.ru/product/1/"), "Санкт-Петербург")
        self.assertIsNone(sites.city_from_url("https://lemanapro.ru/product/x-12345678/"))


class Units(unittest.TestCase):
    def test_names(self):
        cases = {
            "Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг": (30, "кг"),
            "Штукатурка гипсовая ОСНОВИТ Гипсвелл PC21 G": (None, None),  # марка, не фасовка
            "Клей Основит Гранит Т17 25кг.": (25, "кг"),
            "Грунт 0,9 л": (0.9, "л"),
            "Герметик 280 мл": (0.28, "л"),
            "Шпаклевка 500 г": (0.5, "кг"),
            "Краска 2023 г. выпуска 10 л": (10, "л"),
            "Смесь М150 40 кг": (40, "кг"),
        }
        for name, (q, u) in cases.items():
            qty, unit = units.from_text(name)
            if q is None:
                self.assertIsNone(qty, name)
            else:
                self.assertAlmostEqual(qty, q, msg=name)
                self.assertEqual(unit, u, name)

    def test_params(self):
        self.assertEqual(units.pack_of("Смесь", {"Вес, кг": ("30", "")}), (30.0, "кг"))
        self.assertEqual(units.pack_of("Смесь", {"Фасовка": ("25 кг", "")}), (25.0, "кг"))
        self.assertEqual(units.pack_of("Грунт", {"Объем": ("10", "л")}), (10.0, "л"))


class Feeds(unittest.TestCase):
    def test_yml(self):
        f = feeds.read(os.path.join(FIX, "lemanapro_admitad.yml"), "lemanapro")
        self.assertEqual(f.format, "yml")
        self.assertEqual(f.total, 9)
        o = {x["key"]: x for x in f.offers}["lemanapro:82065432"]
        self.assertEqual(o["price"], 590)
        self.assertEqual(o["old_price"], 640)
        self.assertEqual(o["vendor"], "Основит")
        self.assertTrue(o["url"].startswith("https://lemanapro.ru/product/"))
        self.assertEqual(f.chain("111"), ["111", "110", "100"])
        self.assertIsNotNone(f.date)

    def test_filter(self):
        f = feeds.read(
            os.path.join(FIX, "lemanapro_admitad.yml"),
            "lemanapro",
            want=lambda o, feed: "110" in feed.chain(o["category_id"]),
        )
        self.assertEqual(len(f.offers), 8)  # краска не попала
        self.assertEqual(f.categories["111"]["count"], 7)

    def test_csv(self):
        f = feeds.read(os.path.join(FIX, "petrovich_admitad.csv"), "petrovich")
        self.assertEqual(f.format, "csv")
        by = {x["code"]: x for x in f.offers}
        self.assertEqual(by["101201"]["url"], "https://petrovich.ru/product/101201/")
        self.assertEqual(by["101201"]["params"]["Фасовка"][0], "30 кг")
        self.assertEqual(by["101202"]["old_price"], 749)
        self.assertFalse(by["101203"]["available"])

    def test_google(self):
        f = feeds.read(os.path.join(FIX, "google_merchant.xml"), "petrovich")
        self.assertEqual(f.format, "google")
        o = f.offers[0]
        self.assertEqual((o["price"], o["old_price"]), (459, 499))  # цена со скидкой — на полке
        self.assertIn("Стройматериалы > Сухие смеси", f.categories)

    def test_gzip_and_zip(self):
        src = os.path.join(FIX, "lemanapro_admitad.yml")
        gz = os.path.join(TMP, "f.yml.gz")
        with open(src, "rb") as a, gzip.open(gz, "wb") as b:
            b.write(a.read())
        zp = os.path.join(TMP, "f.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.write(src, "feed.yml")
        for p in (gz, zp):
            self.assertEqual(feeds.read(p, "lemanapro").total, 9)

    def test_wrong_network(self):
        with self.assertRaises(feeds.FeedError):
            feeds.read(os.path.join(FIX, "lemanapro_admitad.yml"), "petrovich")

    def test_fetch_local_file_and_errors(self):
        p, _meta = feeds.fetch(os.path.join(FIX, "petrovich_admitad.csv"), os.path.join(TMP, "fd"), "petrovich")
        self.assertTrue(os.path.exists(p))
        with self.assertRaises(feeds.FeedError):
            feeds.fetch("C:/нет/такого.yml", os.path.join(TMP, "fd"), "petrovich")
        with self.assertRaises(feeds.FeedError):
            feeds.fetch("", os.path.join(TMP, "fd"), "petrovich")

    def test_numbers(self):
        self.assertEqual(feeds.parse_number("1 234,50"), 1234.5)
        self.assertEqual(feeds.parse_number("1,234.50"), 1234.5)
        self.assertEqual(feeds.parse_number("690.00 RUB"), 690)
        self.assertIsNone(feeds.parse_number("0"))


class Extract(unittest.TestCase):
    def test_semantic_json(self):
        data = {
            "data": {
                "products": [
                    {
                        "code": 101201,
                        "title": "Штукатурка Основит Гипсвелл 30 кг",
                        "price": {"retail": 579, "gold": 549},
                    },
                    {
                        "code": 101202,
                        "title": "Штукатурка Knauf Ротбанд 30 кг",
                        "price": {"retail": 699, "gold": 680},
                        "old_price": 749,
                    },
                ],
                "delivery": [{"id": 3, "name": "Доставка до двери", "price": 300}],
            }
        }
        items = extract.find_products(data, "petrovich", "https://petrovich.ru/catalog/1546/", "данные страницы")
        by = {i["code"]: i for i in items}
        self.assertEqual(set(by), {"101201", "101202"})  # доставка — не товар
        self.assertEqual(by["101201"]["price"], 579)  # цена на полке, не по карте
        self.assertEqual(by["101202"]["old_price"], 749)

    def test_lemana_like(self):
        data = {
            "content": [
                {
                    "productLmCode": "82065432",
                    "displayedName": "Штукатурка Основит 30 кг",
                    "name": "Штукатурка гипсовая Основит Гипсвелл 30 кг",
                    "productLink": "/product/shtukaturka-osnovit-82065432/",
                    "price": {"displayMain": 590, "displayOld": 640},
                }
            ]
        }
        items = extract.find_products(data, "lemanapro", "https://lemanapro.ru/catalogue/shtukaturki/", "x")
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["price"], items[0]["old_price"]), (590, 640))

    def test_page_layers(self):
        page = {
            "url": "https://lemanapro.ru/catalogue/shtukaturki/",
            "jsonld": [
                {
                    "@type": "Product",
                    "name": "Штукатурка Knauf 30 кг",
                    "sku": "12345678",
                    "url": "https://lemanapro.ru/product/shtukaturka-knauf-12345678/",
                    "offers": {"price": "720", "availability": "https://schema.org/InStock"},
                }
            ],
            "cards": [
                {
                    "url": "https://lemanapro.ru/product/shtukaturka-knauf-12345678/",
                    "name": "Knauf",
                    "price": "719",
                    "old_price": "800",
                },
                {"url": "https://lemanapro.ru/product/volma-12345679/", "name": "Волма Слой 30 кг", "price": "495 ₽"},
            ],
            "state": [],
        }
        items, via = extract.products_from_page(page, [], "lemanapro")
        by = {i["code"]: i for i in items}
        self.assertEqual(by["12345678"]["price"], 720)  # разметка главнее вёрстки
        self.assertEqual(by["12345678"]["old_price"], 800)  # но старую цену дополнила вёрстка
        self.assertEqual(by["12345679"]["via"], "вёрстка")
        self.assertIn("разметка", via)

    def test_check_page(self):
        self.assertTrue(extract.is_check_page({"title": "Проверка браузера", "text": "", "cards": []}))
        self.assertTrue(extract.is_check_page({"title": "", "text": ""}, 401))
        self.assertFalse(extract.is_check_page({"title": "Штукатурки", "text": "Каталог", "cards": [1]}))

    def test_city(self):
        self.assertEqual(extract.city_of({"city": "Ваш город: Санкт-Петербург"}), "Санкт-Петербург")
        self.assertTrue(extract.same_city("Москва", "г. Москва"))
        self.assertFalse(extract.same_city("Москва", "Санкт-Петербург"))


class Pipeline(unittest.TestCase):
    """Два сбора подряд на фидах из файлов: изменения, скачок, на проверку, решения, отчёт."""

    def test_two_runs(self):
        data = os.path.join(TMP, "pipe")
        shutil.rmtree(data, ignore_errors=True)
        os.makedirs(data)
        feed1 = os.path.join(data, "lm.yml")
        shutil.copy(os.path.join(FIX, "lemanapro_admitad.yml"), feed1)
        con = db.connect(os.path.join(data, "m.sqlite"))
        s = config.load()
        s["report_dir"] = os.path.join(data, "reports")
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(feed=feed1, feed_city="Москва", edge_enabled=False)
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        db.add_tracked(con, "lemanapro", "product", "https://lemanapro.ru/product/propal-99999999/", "")

        r1 = collect.run(s, con=con)
        self.assertNotIn("error", r1, r1.get("trace"))
        self.assertEqual(r1["prices"], 8)  # 7 из «Штукатурок» + шпаклёвка Основит из неотслеживаемой категории
        auto = con.execute("SELECT key, found_by FROM products WHERE found_by='auto'").fetchall()
        self.assertEqual([tuple(r) for r in auto], [("lemanapro:66666666", "auto")])  # нашёл автопоиск
        self.assertEqual(r1["sites"]["lemanapro"]["auto"]["ours"], 1)
        kinds = {i["kind"] for i in r1["review_items"]}
        self.assertEqual(kinds, {"pack", "ratio", "missing"})  # «20 л» — свой вид, не ошибка единицы
        ours = next(m for m in r1["market"] if m["key"] == "lemanapro:82065432")
        self.assertEqual(ours["count"], 3)
        self.assertAlmostEqual(ours["median"], 18.2)
        self.assertTrue(os.path.exists(r1["report"]))

        # второй сбор: Knauf подешевел на 10 %, Волма подорожала вдвое
        with open(feed1, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("<price>720</price>", "<price>648</price>").replace(
            "<price>495</price>", "<price>990</price>"
        )
        with open(feed1, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(feed1, None)
        r2 = collect.run(s, con=con)
        changes = {c["key"]: c for c in r2["changes"]}
        self.assertEqual(set(changes), {"lemanapro:12345678", "lemanapro:12345679"})
        self.assertIn("подешевел с 720 до 648 ₽ (−10,0 %)", changes["lemanapro:12345678"]["text"])
        jumps = [i for i in r2["review_items"] if i["kind"] == "jump"]
        self.assertEqual([j["key"] for j in jumps], ["lemanapro:12345679"])
        hist = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        self.assertEqual(hist, 8 + 2)  # история пишется только при изменении (8 товаров, 2 изменения)

        # решения: «не конкурент» и «всё верно» убирают из «На проверку»
        db.decide(con, "lemanapro:12345681", "exclude")
        db.decide(con, "lemanapro:12345679", "accept:" + jumps[0]["signature"])
        r3 = collect.run(s, con=con)
        keys = {i["key"] for i in r3["review_items"]}
        self.assertNotIn("lemanapro:12345681", keys)
        self.assertNotIn("lemanapro:12345679", keys)
        self.assertEqual(r3["changes"], [])

        with zipfile.ZipFile(r3["report"]) as z:
            wbxml = z.read("xl/workbook.xml").decode("utf-8")
            self.assertEqual(len([n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]), 10)
            self.assertTrue(any(n.startswith("xl/charts/chart") for n in z.namelist()))
        for name in (
            "Изменения цен",
            "Основит и рынок",
            "Ассортимент",
            "На проверку",
            "Динамика",
            "Сверка фида",
            "Все цены",
            "История",
            "Динамика — данные",
            "Сводка",
        ):
            self.assertIn(f'name="{name}"', wbxml)
        con.close()

    def test_brand(self):
        self.assertEqual(analysis.is_ours("Штукатурка Основит", "Основит", ["Основит"]), (True, False))
        self.assertEqual(analysis.is_ours("Аналог Основит", "Knauf", ["Основит"]), (False, True))
        self.assertEqual(analysis.is_ours("OSNOVIT PC21", "", ["Основит", "Osnovit"]), (True, False))


class Kinds(unittest.TestCase):
    def test_detect(self):
        cases = {
            "Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг": "Штукатурка гипсовая",
            "Шпатлевка полимерная финишная Vetonit LR+ 20 кг": "Шпаклёвка полимерная финишная",
            "Клей для плитки Ceresit CM 11 Plus C1 T 25 кг": "Клей плиточный C1",
            "Клей для керамогранита усиленный С2 TE 25кг": "Клей плиточный C2",  # кириллическая «С»
            "Клей для газобетона Основит Селформ Т112 25 кг": "Клей для блоков",
            "Ровнитель для пола финишный 25 кг": "Наливной пол финишный",
            "Пескобетон М300 40 кг": "Пескобетон М300",
            "Плитка керамическая": None,
        }
        for name, want in cases.items():
            self.assertEqual(kinds.detect(name), want, name)

    def test_compatible(self):
        ours = kinds.attrs("Шпаклёвка финишная Основит Эконсилк PG34 W 20 кг")  # основа не указана
        self.assertTrue(kinds.compatible(ours, kinds.attrs("Шпаклевка полимерная финишная Ceresit 20 кг")))
        self.assertTrue(kinds.compatible(ours, kinds.attrs("Шпаклевка гипсовая финишная 20 кг")))
        self.assertFalse(kinds.compatible(ours, kinds.attrs("Шпаклевка гипсовая стартовая 25 кг")))
        self.assertFalse(kinds.compatible(kinds.attrs("Клей плиточный C1"), kinds.attrs("Клей плиточный C2")))
        self.assertFalse(kinds.compatible(kinds.attrs("Штукатурка гипсовая"), kinds.attrs("Шпаклёвка гипсовая")))

    def test_override(self):
        p = {"key": "k", "name": "Смесь Основит XYZ 25 кг"}
        self.assertEqual(kinds.attrs_of(p, {}), ({}, False))
        a, manual = kinds.attrs_of(p, {"k": "Штукатурка цементная"})
        self.assertTrue(manual)
        self.assertEqual(a, {"type": "Штукатурка", "base": "цементная"})


class Analogs(unittest.TestCase):
    """Основит сравнивается с аналогами, а не со всем разделом."""

    def _p(self, key, name, price, qty, ours=False, gid=1):
        return {
            "key": key,
            "site": "lemanapro",
            "name": name,
            "price": price,
            "pack_qty": qty,
            "pack_unit": "кг",
            "is_ours": ours,
            "group_id": gid,
            "available": 1,
            "url": "",
            "source": "feed",
        }

    def test_levels(self):
        s = config.load()
        prods = [
            self._p("o1", "Штукатурка гипсовая Основит 30 кг", 600, 30, True),
            self._p("c1", "Штукатурка гипсовая А 30 кг", 540, 30),
            self._p("c2", "Штукатурка гипсовая Б 30 кг", 570, 30),
            self._p("c3", "Штукатурка гипсовая В 30 кг", 630, 30),
            self._p("c4", "Штукатурка цементная Г 25 кг", 300, 25),  # дешевле, но не аналог
            self._p("c5", "Штукатурка цементная Д 25 кг", 320, 25),
            self._p("o2", "Шпаклёвка финишная Основит 20 кг", 600, 20, True),
            self._p("c6", "Шпаклевка гипсовая финишная 20 кг", 500, 20),
            self._p("o3", "Смесь Основит Особая 25 кг", 500, 25, True),  # вид не распознан
        ]
        market, review, _ = analysis.analyse(prods, {1: "Сухие смеси"}, s, {}, (), {})
        by = {r["key"]: r for r in market}
        self.assertEqual(by["o1"]["level"], "аналоги")
        self.assertEqual(by["o1"]["count"], 3)
        self.assertAlmostEqual(by["o1"]["median"], 19.0)  # только гипсовые
        self.assertEqual(by["o2"]["level"], "аналоги")  # аналог один — но лучше, чем весь раздел
        self.assertEqual(by["o2"]["count"], 1)
        self.assertEqual(by["o3"]["level"], "раздел")  # вид не распознан — раздел
        self.assertIn("kind", {i["kind"] for i in review if i["key"] == "o3"})
        # ручной вид: o3 — цементная штукатурка, сравнение с Г и Д
        market, _, _ = analysis.analyse(prods, {1: "Сухие смеси"}, s, {}, (), {"o3": "Штукатурка цементная"})
        o3 = {r["key"]: r for r in market}["o3"]
        self.assertEqual((o3["kind"], o3["level"], o3["count"]), ("Штукатурка цементная", "аналоги", 2))


class Crosscheck(unittest.TestCase):
    def test_pick_and_summary(self):
        found = {
            f"s:{i}": {
                "key": f"s:{i}",
                "url": f"https://lemanapro.ru/product/x-{i}/",
                "code": str(i),
                "source": "feed",
                "name": "Штукатурка" + (" Основит" if i < 2 else ""),
                "vendor": "",
                "price": 100.0,
            }
            for i in range(20)
        }
        s = config.load()
        s["crosscheck"]["n"] = 4
        picks = collect._pick_checks(found, s, run_id=7)
        self.assertEqual(len(picks), 12)  # с запасом ×3
        self.assertEqual({p[0] for p in picks[:2]}, {"s:0", "s:1"})  # наши — первыми
        self.assertEqual(picks, collect._pick_checks(found, s, run_id=7))  # в одном сборе одинаково
        self.assertNotEqual(picks, collect._pick_checks(found, s, run_id=8))  # в разных — по кругу
        s["crosscheck"]["n"] = 0
        self.assertEqual(collect._pick_checks(found, s, 7), [])
        summ = collect._crosscheck_summary([("x", "s:0", 100.0, 110.0), ("x", "s:1", 100.0, 100.0)], found)
        self.assertEqual((summ["n"], summ["avg_pct"], summ["over5"], summ["same"]), (2, 5.0, 1, 1))


class XlsxCharts(unittest.TestCase):
    def test_chart_parts(self):
        d = xlsx.Sheet("Данные", [("Сбор", 10, xlsx.TEXT), ("A", 10, xlsx.MONEY), ("B", 10, xlsx.MONEY)])
        for i in range(3):
            d.add(f"0{i}.10", 10.0 + i, 12.0)
        c = xlsx.Sheet("Графики", [("", 9, xlsx.TEXT)] * 8)
        c.header = False
        c.add("подпись")
        c.add_chart(
            "Тест",
            ("Данные", 0, 2, 4),
            [("A", "Данные", 1, 2, 4, "2A78D6"), ("B", "Данные", 2, 2, 4, "EB6834")],
            anchor=(0, 1, 8, 16),
            y_title="₽/кг",
        )
        path = os.path.join(TMP, "chart.xlsx")
        xlsx.write(path, [c, d])
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            self.assertIn("xl/charts/chart1.xml", names)
            self.assertIn("xl/drawings/drawing1.xml", names)
            self.assertIn("xl/worksheets/_rels/sheet1.xml.rels", names)
            chart = z.read("xl/charts/chart1.xml").decode("utf-8")
            self.assertIn("'Данные'!$B$2:$B$4", chart)
            self.assertIn('<a:prstDash val="dash"/>', chart)  # вторая серия — пунктир
            self.assertIn("drawingml.chart+xml", z.read("[Content_Types].xml").decode("utf-8"))
            import xml.dom.minidom

            for n in names:
                if n.endswith(".xml") or n.endswith(".rels"):
                    xml.dom.minidom.parseString(z.read(n))  # каждый файл — корректный XML


class Assortment(unittest.TestCase):
    def test_new_gone_out_back(self):
        data = os.path.join(TMP, "asm")
        shutil.rmtree(data, ignore_errors=True)
        os.makedirs(data)
        feed = os.path.join(data, "lm.yml")
        with open(os.path.join(FIX, "lemanapro_admitad.yml"), encoding="utf-8") as f:
            base = f.read()
        with open(feed, "w", encoding="utf-8") as f:
            f.write(base)
        con = db.connect(os.path.join(data, "m.sqlite"))
        s = config.load()
        s["report_dir"] = os.path.join(data, "reports")
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(feed=feed, edge_enabled=False)
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        con.execute("UPDATE tracked SET added=added-10")
        r1 = collect.run(s, con=con)
        self.assertEqual(r1["assortment"], [])  # первый сбор — сравнивать не с чем
        import re as _re

        text = _re.sub(r'\s*<offer id="12345679".*?</offer>', "", base, flags=_re.S)  # Волма пропала
        text = text.replace('<offer id="12345680" available="false">', '<offer id="12345680" available="true">')
        text = text.replace('<offer id="12345678" available="true">', '<offer id="12345678" available="false">')
        text = text.replace(
            "    </offers>",
            """      <offer id="12340000" available="true">
        <url>https://lemanapro.ru/product/novaya-12340000/</url><price>600</price><currencyId>RUR</currencyId>
        <categoryId>111</categoryId><name>Штукатурка гипсовая Новая 30 кг</name><vendor>Новая</vendor></offer>
    </offers>""",
        )
        with open(feed, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(feed, (time.time() + 5, time.time() + 5))
        r2 = collect.run(s, con=con)
        ev = {(e["type"], e["key"]) for e in r2["assortment"]}
        self.assertEqual(
            ev,
            {
                ("gone", "lemanapro:12345679"),
                ("back", "lemanapro:12345680"),
                ("out", "lemanapro:12345678"),
                ("new", "lemanapro:12340000"),
            },
        )
        # фид не прочитался — «пропавших» нет, даже если товаров не видно
        s["sites"]["lemanapro"]["feed"] = os.path.join(data, "нет.yml")
        r3 = collect.run(s, con=con)
        self.assertEqual(r3["assortment"], [])
        con.close()


class NamesCorpus(unittest.TestCase):
    """Вид и фасовка на наборе типичных названий DIY-сетей (tests/fixtures/names.tsv)."""

    def test_corpus(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "names.tsv")
        misses = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                name, kind, pack = [*line.rstrip("\n").split("\t"), "", ""][:3]
                got_kind = kinds.detect(name) or ""
                qty, unit = units.pack_of(name, None, kinds.pack_unit(name))
                got_pack = f"{qty:g} {unit}" if qty else ""
                if got_kind != kind or got_pack != pack:
                    misses.append(f"{name}: вид «{got_kind}» (ждали «{kind}»), фасовка «{got_pack}» (ждали «{pack}»)")
        self.assertEqual(misses, [], "\n".join(misses))


class Portability(unittest.TestCase):
    def test_strftime_has_ascii_format(self):
        # На Windows с Python 3.10 strftime с кириллицей в формате падает (UnicodeEncodeError, 'locale' codec)
        import re

        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor")
        bad = []
        for name in sorted(os.listdir(root)):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    for n, line in enumerate(f, 1):
                        for fmt in re.findall(r"strftime\(f?([\"'])(.*?)\1", line):
                            if any(ord(c) > 127 for c in fmt[1]):
                                bad.append(f"{name}:{n}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
