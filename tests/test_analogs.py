"""Автопоиск товаров Основит и их аналогов по всему фиду."""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-analogs-")

from monitor import analogs, collect, config, db, report  # noqa: E402

BRAND = ["Основит", "Osnovit"]


def offer(code, name, price=500.0, vendor="", available=True):
    return {
        "key": f"lemanapro:{code}",
        "code": code,
        "name": name,
        "price": price,
        "vendor": vendor,
        "available": available,
        "currency": "RUB",
        "params": {},
    }


class Wanted(unittest.TestCase):
    def test_candidates(self):
        self.assertTrue(analogs.wanted(offer("1", "Затирка цементная Церезит CE33 2 кг"), None))
        self.assertFalse(analogs.wanted(offer("2", "Ведро строительное 12 л"), None))  # вид не наш
        self.assertFalse(analogs.wanted({**offer("3", "Затирка 2 кг"), "price": None}, None))
        self.assertFalse(analogs.wanted({**offer("4", "Затирка 2 кг"), "currency": "USD"}, None))
        self.assertFalse(analogs.wanted(offer("5", "Затирка цементная 2 кг"), {"Штукатурка"}))  # только выбранные виды


class Prefilter(unittest.TestCase):
    def test_never_drops_a_known_kind(self):
        """Быстрый отсев не должен терять ни одного названия, которое распознаёт полный разбор."""
        from monitor import kinds

        with open(os.path.join(FIX, "names.tsv"), encoding="utf-8") as f:
            names = [ln.split("\t")[0] for ln in f if ln.strip() and not ln.startswith("#")]
        names += [k for k in kinds.all_labels()]
        for n in names:
            with self.subTest(name=n):
                self.assertTrue(not kinds.attrs(n) or kinds.maybe_kind(n))
        self.assertFalse(kinds.maybe_kind("Дрель ударная Бош 750 Вт"))


class Pick(unittest.TestCase):
    def test_ours_and_analogs_by_kind(self):
        tracked = {o["key"]: o for o in [offer("10", "Штукатурка гипсовая Основит Гипсвелл 30 кг", vendor="Основит")]}
        cands = [
            offer("20", "Затирка цементная Основит Плитсэйв XC6 2 кг", vendor="Основит"),  # Основит вне отслеживаемого
            offer("21", "Затирка цементная Церезит CE33 2 кг"),  # аналог затирки
            offer("22", "Затирка эпоксидная Литокол 2,5 кг"),  # тот же вид, другая основа — после настоящих
            offer("23", "Затирка цементная без фасовки"),  # без фасовки — не берём
            offer("24", "Штукатурка гипсовая Кнауф Ротбанд 30 кг"),  # аналог штукатурки
            offer("25", "Грунтовка глубокого проникновения 10 л"),  # у Основит нет грунтовки — не ищем
            offer("26", "Штукатурка гипсовая Волма 30 кг", available=False),
            offer("10", "Штукатурка гипсовая Основит Гипсвелл 30 кг", vendor="Основит"),  # уже отслеживается
        ]
        out, summary = analogs.pick(cands, tracked, BRAND, max_per_kind=40)
        keys = [o["key"].split(":")[1] for o in out]
        self.assertEqual(keys[0], "20")  # сначала товары Основит
        self.assertEqual(set(keys), {"20", "21", "22", "24", "26"})
        self.assertLess(keys.index("21"), keys.index("22"))  # настоящий аналог раньше «того же вида»
        self.assertLess(keys.index("24"), keys.index("26"))  # в наличии раньше
        self.assertEqual(summary["ours"], 1)
        self.assertEqual(summary["kinds"]["Затирка"], {"found": 2, "taken": 2})
        self.assertEqual(summary["kinds"]["Штукатурка"], {"found": 2, "taken": 2})

    def test_limit_per_kind(self):
        tracked = {"lemanapro:1": offer("1", "Затирка цементная Основит 2 кг", vendor="Основит")}
        cands = [offer(str(100 + i), f"Затирка цементная Марка{i} 2 кг") for i in range(30)]
        out, summary = analogs.pick(cands, tracked, BRAND, max_per_kind=5)
        self.assertEqual(len(out), 5)
        self.assertEqual(summary["kinds"]["Затирка"], {"found": 30, "taken": 5})

    def test_unit_must_match(self):
        """Наш товар продаётся в кг — аналог в литрах за кг не сравнить."""
        tracked = {"lemanapro:1": offer("1", "Затирка цементная Основит 2 кг", vendor="Основит")}
        out, _ = analogs.pick([offer("2", "Затирка готовая Церезит 1 л")], tracked, BRAND)
        self.assertEqual(out, [])

    def test_no_ours_no_search(self):
        out, summary = analogs.pick([offer("2", "Затирка цементная Церезит 2 кг")], {}, BRAND)
        self.assertEqual((out, summary["ours"], summary["kinds"]), ([], 0, {}))


class Pipeline(unittest.TestCase):
    def run_once(self, auto):
        data = tempfile.mkdtemp(prefix="osnovit-ap-")
        feed = os.path.join(data, "lm.yml")
        shutil.copy(os.path.join(FIX, "lemanapro_admitad.yml"), feed)
        con = db.connect(os.path.join(data, "m.sqlite"))
        self.addCleanup(con.close)
        s = config.load()
        s["report_dir"] = os.path.join(data, "reports")
        s["sites"]["petrovich"]["enabled"] = False
        s["sites"]["lemanapro"].update(feed=feed, feed_city="Москва", edge_enabled=False)
        s["analogs"]["auto"] = auto
        db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
        return collect.run(s, con=con), con

    def test_auto_finds_ours_outside_tracked(self):
        r, con = self.run_once(True)
        self.assertNotIn("error", r, r.get("trace"))
        auto = dict(con.execute("SELECT key, found_by FROM products WHERE found_by='auto'").fetchall())
        self.assertEqual(set(auto), {"lemanapro:66666666"})  # шпаклёвка Основит из «Шпаклёвок»
        row = next(m for m in r["market"] if m["key"] == "lemanapro:66666666")
        self.assertEqual(row["group"], "автопоиск")
        self.assertNotIn("lemanapro:66666666", {e["key"] for e in r["assortment"]})  # не шумит в ассортименте

    def test_auto_off(self):
        r, con = self.run_once(False)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM products WHERE found_by='auto'").fetchone()[0], 0)
        self.assertNotIn("auto", r["sites"]["lemanapro"])

    def test_report_marks_auto(self):
        self.assertEqual(report._group({"found_by": "auto", "group_id": None}, {}), "Основит и аналоги — автопоиск")
        self.assertEqual(report._group({"found_by": "tracked", "group_id": 1}, {1: "Штукатурки"}), "Штукатурки")


if __name__ == "__main__":
    unittest.main()
