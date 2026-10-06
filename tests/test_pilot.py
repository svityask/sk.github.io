"""2.4: ручная фасовка, выборочная проверка, расписание из XML, самопроверка, копия бэкапов во вторую папку."""

import os
import sys
import tempfile
import time
import unittest
from typing import ClassVar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-pilot-")

import http.server  # noqa: E402
import threading  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402

from monitor import backup, config, db, health, schedule, selftest, spot  # noqa: E402


def fresh_db(name):
    """Своя папка на каждый вызов: на Windows открытую базу не удалить, поэтому папки не переиспользуем."""
    base = os.path.join(config.DATA, "pilot")
    os.makedirs(base, exist_ok=True)
    d = tempfile.mkdtemp(prefix=name + "-", dir=base)
    return db.connect(os.path.join(d, "m.sqlite")), d


def item(code, name, price, site="lemanapro", vendor=""):
    from monitor import kinds, units

    qty, unit = units.pack_of(name, None, kinds.pack_unit(name))
    return {
        "key": f"{site}:{code}",
        "site": site,
        "code": code,
        "name": name,
        "url": f"https://example.test/{code}",
        "vendor": vendor,
        "pack_qty": qty,
        "pack_unit": unit,
        "price": price,
        "source": "feed",
        "city": "Москва",
    }


def fill(con, n_per_site=20, ours=4):
    for site in ("lemanapro", "petrovich"):
        for i in range(n_per_site):
            db.record(con, 1, item(f"{i}", f"Штукатурка гипсовая Марка{i} 30 кг", 400 + i, site), time.time())
    for i in range(ours):
        db.record(con, 1, item(f"o{i}", f"Штукатурка гипсовая Основит Гипсвелл {i} 30 кг", 500, vendor="Основит"))


class PackOverride(unittest.TestCase):
    def test_manual_pack_wins_and_survives_next_run(self):
        con, _ = fresh_db("pack")
        self.addCleanup(con.close)
        db.record(con, 1, item("1", "Шпаклевка готовая Марка", 900))  # фасовки в названии нет
        self.assertIsNone(con.execute("SELECT pack_qty FROM products").fetchone()[0])
        self.assertEqual(db.set_pack(con, "lemanapro:1", "18 кг"), (18.0, "кг"))
        self.assertEqual(con.execute("SELECT pack_qty, pack_unit FROM products").fetchone()[:], (18.0, "кг"))
        db.record(con, 2, item("1", "Шпаклевка готовая Марка", 950))  # новый сбор не затирает ручную фасовку
        self.assertEqual(con.execute("SELECT pack_qty FROM products").fetchone()[0], 18.0)
        with self.assertRaises(ValueError):
            db.set_pack(con, "lemanapro:1", "мешок")
        db.set_pack(con, "lemanapro:1", "")  # вернуть автоматическую
        self.assertIsNone(con.execute("SELECT pack_qty FROM products").fetchone()[0])
        self.assertEqual(db.pack_overrides(con), {})


class SpotCheck(unittest.TestCase):
    def setUp(self):
        self.con, _ = fresh_db("spot")
        self.addCleanup(self.con.close)
        self.settings = config.load()
        self.settings["brand_words"] = ["основит"]
        fill(self.con)

    def test_sample_is_balanced(self):
        keys = spot.new_sample(self.con, self.settings, n=12, seed=1)
        self.assertEqual(len(keys), 12)
        self.assertEqual(len(set(keys)), 12)
        ours = [k for k in keys if ":o" in k]
        self.assertEqual(len(ours), 4)  # до трети — Основит
        lem = [k for k in keys if k.startswith("lemanapro:") and ":o" not in k]
        pet = [k for k in keys if k.startswith("petrovich:")]
        self.assertEqual((len(lem), len(pet)), (4, 4))  # остальное поровну по сетям

    def test_marks_fix_and_count(self):
        keys = spot.new_sample(self.con, self.settings, n=6, seed=2)
        spot.mark(self.con, self.settings, keys[0], "ok")
        spot.mark(self.con, self.settings, keys[1], "pack", "25 кг")
        spot.mark(self.con, self.settings, keys[2], "kind", "Штукатурка цементная")
        spot.mark(self.con, self.settings, keys[3], "not_competitor")
        spot.mark(self.con, self.settings, keys[4], "price", "455")
        v = spot.view(self.con, self.settings)
        by = {i["key"]: i for i in v["items"]}
        self.assertEqual(by[keys[1]]["pack"], "25 кг")
        self.assertTrue(by[keys[1]]["pack_manual"])
        self.assertEqual(by[keys[2]]["kind"], "Штукатурка цементная")
        self.assertTrue(by[keys[3]]["excluded"])
        self.assertEqual(by[keys[0]]["check"]["verdict"], "ok")
        self.assertIsNone(by[keys[5]]["check"])
        st = v["stats"]
        self.assertEqual((st["total"], st["ok"], st["accuracy"]), (5, 1, 20))
        # переотметка того же товара считается один раз — последней отметкой
        spot.mark(self.con, self.settings, keys[4], "ok")
        self.assertEqual(spot.stats(self.con)["ok"], 2)
        spot.undo(self.con, keys[4])
        self.assertIsNone({i["key"]: i for i in spot.view(self.con, self.settings)["items"]}[keys[4]]["check"])
        with self.assertRaises(ValueError):
            spot.mark(self.con, self.settings, keys[5], "pack", "непонятно")

    def test_checked_items_skip_next_sample(self):
        keys = spot.new_sample(self.con, self.settings, n=10, seed=3)
        for k in keys:
            spot.mark(self.con, self.settings, k, "ok")
        again = spot.new_sample(self.con, self.settings, n=10, seed=4)
        self.assertFalse(set(keys) & set(again))


class ScheduleXml(unittest.TestCase):
    NS: ClassVar[dict[str, str]] = {"t": schedule.NS}

    def parse(self, xml):
        # в заголовке encoding="UTF-16" — ElementTree со строкой это не устраивает, разбираем без него
        return ET.fromstring(xml.split("\n", 1)[1])

    def test_daily_run(self):
        root = self.parse(schedule.build_xml("run", "07:45", wake=True, command=r"C:\Монитор\Запустить.cmd"))
        g = lambda path: root.find(path, self.NS).text  # noqa: E731
        self.assertTrue(g("t:Triggers/t:CalendarTrigger/t:StartBoundary").endswith("T07:45:00"))
        self.assertEqual(g("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"), "1")
        self.assertEqual(g("t:Settings/t:StartWhenAvailable"), "true")  # пропущенный запуск — сразу после включения
        self.assertEqual(g("t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertEqual(g("t:Settings/t:WakeToRun"), "true")
        self.assertEqual(g("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(g("t:Actions/t:Exec/t:Command"), r"C:\Монитор\Запустить.cmd")
        self.assertEqual(g("t:Actions/t:Exec/t:Arguments"), "--run")

    def test_watchdog_every_6h_never_wakes(self):
        root = self.parse(schedule.build_xml("watchdog", wake=True))
        self.assertEqual(root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", self.NS).text, "PT6H")
        self.assertEqual(root.find("t:Settings/t:WakeToRun", self.NS).text, "false")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", self.NS).text, "--watchdog")

    @unittest.skipUnless(sys.platform.startswith("win") and os.environ.get("CI"), "только в CI на Windows")
    def test_real_task_scheduler(self):
        try:
            r = schedule.set_schedule(True, "08:30", watchdog=True, wake=False)
            self.assertTrue(r["ok"], r)
            st = schedule.state()
            self.assertTrue(st["enabled"] and st["watchdog"], st)
            q = schedule._schtasks("/Query", "/TN", schedule.TASK_NAME, "/XML")
            self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", q.stdout)
            # задача по-старому (одной строкой schtasks, как до 2.4) пересоздаётся из XML с тем же временем
            schedule._schtasks("/Delete", "/TN", schedule.TASK_NAME, "/F")
            old = schedule._schtasks(
                "/Create",
                "/TN",
                schedule.TASK_NAME,
                "/TR",
                f'"{schedule.launcher()}" --run',
                "/SC",
                "DAILY",
                "/ST",
                "07:10",
                "/F",
            )
            self.assertEqual(old.returncode, 0, old.stderr)
            self.assertTrue(schedule.upgrade())
            q = schedule._schtasks("/Query", "/TN", schedule.TASK_NAME, "/XML").stdout
            self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", q)
            self.assertIn("T07:10:00", q)
            self.assertFalse(schedule.upgrade())  # второй раз — нечего пересоздавать
        finally:
            schedule.set_schedule(False)
        self.assertFalse(schedule.state()["enabled"])


class SelfTest(unittest.TestCase):
    def test_checks_and_network(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.send_response(200 if self.path == "/feed.xml" else 404)
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = config.load()
            port = srv.server_address[1]
            s["sites"]["lemanapro"].update(feed=f"http://127.0.0.1:{port}/feed.xml", edge_enabled=False)
            s["sites"]["petrovich"].update(feed=f"http://127.0.0.1:{port}/nope.xml", edge_enabled=False)
            r = selftest.run(s, None)
        finally:
            srv.shutdown()
        by = {c["name"]: c for c in r["checks"]}
        self.assertEqual(by["Python"]["status"], "ok")
        self.assertEqual(by["Папка данных"]["status"], "ok")
        self.assertEqual(by["Фид: Лемана ПРО"]["status"], "ok")
        self.assertEqual(by["Фид: Петрович"]["status"], "fail")
        self.assertIn("404", by["Фид: Петрович"]["detail"])
        self.assertEqual(r["status"], "fail")


class BackupMirror(unittest.TestCase):
    def test_copy_goes_to_second_folder(self):
        con, d = fresh_db("mirror")
        self.addCleanup(con.close)
        s = config.load()
        s["backup"]["mirror_dir"] = os.path.join(d, "Облако")
        db.record(con, 1, item("1", "Штукатурка гипсовая 30 кг", 400))
        path = backup.make(con, "тест", s)
        mirrored = os.path.join(backup.mirror_folder(s), os.path.basename(path))
        self.assertTrue(os.path.exists(mirrored))
        # недоступная вторая папка не мешает локальной копии
        s["backup"]["mirror_dir"] = os.path.join(d, "file-not-dir")
        open(s["backup"]["mirror_dir"], "w").close()
        time.sleep(1.1)
        self.assertTrue(backup.make(con, "тест", s))
        con.execute(
            "INSERT INTO runs(started, finished, status, summary) VALUES(?,?,?,?)", (1, time.time(), "готово", "{}")
        )
        h = {c["name"]: c for c in health.check(con, s)["checks"]}
        self.assertEqual(h["Копия во второй папке"]["status"], "warn")


if __name__ == "__main__":
    unittest.main()
