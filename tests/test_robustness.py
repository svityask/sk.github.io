"""Исправления 2.4.1: WebSocket с большими сообщениями, числа и даты фида, CSV потоком, ошибки окна, сбой одной сети."""

import gzip
import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-rob-")

from monitor import cdp, collect, config, db, feeds, kinds, schedule, server, xlsx  # noqa: E402
from monitor.feeds import MSK  # noqa: E402


def frame(text: str) -> bytes:
    """Текстовый кадр сервера (без маски), как его шлёт браузер."""
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        head = bytes([0x81, n])
    elif n < 65536:
        head = bytes([0x81, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x81, 127]) + struct.pack(">Q", n)
    return head + data


def socket_ws():
    """WebSocket поверх пары сокетов — без рукопожатия и без браузера."""
    a, b = socket.socketpair()
    ws = object.__new__(cdp.WebSocket)
    ws.sock = a
    ws.buf = bytearray()
    return ws, b


class WebSocketFrames(unittest.TestCase):
    def test_slow_big_message_is_not_lost(self):
        """Заголовок кадра пришёл, тело — позже короткого тайм-аута: сообщение дочитывается, а не теряется."""
        ws, peer = socket_ws()
        big = json.dumps({"id": 1, "result": {"body": "x" * 300_000}})
        data = frame(big) + frame('{"id": 2}')

        def slow():
            peer.sendall(data[:10])  # заголовок и чуть-чуть тела
            time.sleep(0.5)
            peer.sendall(data[10:])

        t = threading.Thread(target=slow)
        t.start()
        try:
            msg = ws.recv(timeout=0.05)
            while msg is None:  # первый байт мог не успеть
                msg = ws.recv(timeout=0.05)
            self.assertEqual(json.loads(msg)["id"], 1)
            self.assertEqual(len(json.loads(msg)["result"]["body"]), 300_000)
            self.assertEqual(json.loads(ws.recv(timeout=2))["id"], 2)  # поток кадров не сбился
        finally:
            t.join()
            peer.close()
            ws.sock.close()

    def test_timeout_without_data(self):
        ws, peer = socket_ws()
        try:
            self.assertIsNone(ws.recv(timeout=0.05))
            peer.sendall(frame("привет"))
            self.assertEqual(ws.recv(timeout=2), "привет")
        finally:
            peer.close()
            ws.sock.close()

    def test_fragmented_message_and_ping(self):
        ws, peer = socket_ws()
        try:
            peer.sendall(bytes([0x01, 3]) + b"abc")  # начало, без FIN
            peer.sendall(bytes([0x89, 0]))  # ping посередине
            peer.sendall(bytes([0x80, 3]) + b"def")  # продолжение с FIN
            self.assertEqual(ws.recv(timeout=2), "abcdef")
            pong = peer.recv(16)
            self.assertEqual(pong[0], 0x8A)
        finally:
            peer.close()
            ws.sock.close()

    def test_broken_json_is_skipped(self):
        self.assertEqual(cdp._loads("{oops"), {})
        self.assertEqual(cdp._loads("[1, 2]"), {})
        self.assertEqual(cdp._loads('{"id": 3}'), {"id": 3})


class Numbers(unittest.TestCase):
    def test_prices(self):
        cases = {
            "1 234,50": 1234.5,
            "1,234.50": 1234.5,
            "1.234,50": 1234.5,  # европейская запись — раньше получалось 1,2345
            "1.234.567": 1234567,
            "1,234,567": 1234567,
            "1 234": 1234,
            "12,5": 12.5,
            "990 ₽": 990,
            "690.00 RUB": 690,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(feeds.parse_number(text), want)
        for text in ("0", "", "abc", None, "-5"):
            with self.subTest(text=text):
                self.assertIsNone(feeds.parse_number(text))

    def test_dates_without_zone_are_moscow(self):
        moscow_8am = 1791262800.0  # 2026-10-06 08:00 MSK = 05:00 UTC
        self.assertEqual(feeds.parse_date("2026-10-06 08:00"), moscow_8am)
        self.assertEqual(feeds.parse_date("2026-10-06T08:00:00.000"), moscow_8am)  # не по поясу компьютера
        self.assertEqual(feeds.parse_date("2026-10-06T05:00:00Z"), moscow_8am)
        self.assertEqual(feeds.parse_date("2026-10-06T08:00:00+03:00"), moscow_8am)
        self.assertEqual(MSK.utcoffset(None).total_seconds(), 3 * 3600)


class CsvStream(unittest.TestCase):
    def write(self, name, text, enc="utf-8", gz=False):
        d = tempfile.mkdtemp(prefix="osnovit-csv-")
        p = os.path.join(d, name)
        data = text.encode(enc)
        if gz:
            data = gzip.compress(data)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def rows(self, n):
        out = ["id;name;price;url;category"]
        for i in range(n):
            out.append(
                f'{i};"Штукатурка гипсовая Основит №{i}; 30 кг";{100 + i},50;'
                f"https://petrovich.ru/product/{100000 + i}/;Сухие смеси > Штукатурки"
            )
        return "\n".join(out) + "\n"

    def test_cp1251_many_rows(self):
        p = self.write("feed.csv", self.rows(2000), enc="cp1251")
        f = feeds.read(p, "petrovich")
        self.assertEqual(f.total, 2000)
        self.assertEqual(f.offers[0]["name"], "Штукатурка гипсовая Основит №0; 30 кг")
        self.assertEqual(f.offers[-1]["price"], 2099.5)
        self.assertIn("Сухие смеси > Штукатурки", f.categories)

    def test_gzip_utf8_with_bom_and_multiline_field(self):
        text = '﻿id,name,price,url\n1,"Клей\nплиточный 25 кг",450,https://petrovich.ru/product/123456/\n'
        p = self.write("feed.csv", text, gz=True)
        f = feeds.read(p, "petrovich", fmt="csv")
        self.assertEqual(f.total, 1)
        self.assertEqual(f.offers[0]["name"], "Клей плиточный 25 кг")
        self.assertEqual(f.offers[0]["price"], 450)

    def test_utf8_letter_cut_at_probe_border(self):
        """Граница пробы попала в середину русской буквы — это всё равно UTF-8, а не cp1251."""
        head = "id;name;price\n1;" + "x" * (feeds.CSV_PROBE_BYTES - 20)
        text = head + "ЖЖЖЖЖЖЖЖЖЖЖЖ;100\n"
        p = self.write("feed.csv", text)
        cut = feeds.CSV_PROBE_BYTES
        with open(p, "rb") as fh:
            self.assertGreater(fh.read()[cut - 1], 0x7F)  # проба режет букву
        self.assertEqual(feeds._csv_encoding(p), "utf-8-sig")


class WindowErrors(unittest.TestCase):
    def test_unexpected_error_is_500_json(self):
        class Broken:
            token = "t"
            last_ping = 0.0

            def state(self):
                raise RuntimeError("сломалось")

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(Broken()))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}"
            req = urllib.request.Request(url + "/api/state", headers={"X-Token": "t"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 500)
            self.assertIn("сломалось", json.loads(cm.exception.read().decode("utf-8"))["error"])
            # сервер жив и отвечает дальше
            req = urllib.request.Request(url + "/api/nope", headers={"X-Token": "t"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


class OneSiteFails(unittest.TestCase):
    def collector(self):
        c = collect.Collector(config.load(), collect.Status(), False, False, None, "test")
        c.summary = {"sites": {}, "warnings": [], "changes": [], "assortment": []}
        return c

    def test_other_site_goes_on(self):
        c = self.collector()
        done = []

        def site(name, conf):
            if name == "petrovich":
                raise KeyError("price")
            done.append(name)

        c._site = site
        c._site_safe("petrovich", {})
        c._site_safe("lemanapro", {})
        self.assertEqual(done, ["lemanapro"])
        self.assertIn("KeyError", c.summary["sites"]["petrovich"]["error"])
        self.assertTrue(any("Петрович" in w for w in c.summary["warnings"]))

    def test_database_error_stops_the_run(self):
        c = self.collector()

        def site(name, conf):
            raise sqlite3.OperationalError("database is locked")

        c._site = site
        with self.assertRaises(sqlite3.OperationalError):
            c._site_safe("petrovich", {})


class ScheduleTime(unittest.TestCase):
    def test_parse_at(self):
        self.assertEqual(schedule.parse_at("08:30"), (8, 30))
        self.assertEqual(schedule.parse_at("8.05"), (8, 5))
        self.assertEqual(schedule.parse_at(" 2359 "), (23, 59))
        for bad in ("25:00", "08:61", "утром", "8"):
            with self.subTest(at=bad), self.assertRaises(ValueError):
                schedule.parse_at(bad)

    def test_xml_rejects_bad_time(self):
        with self.assertRaises(ValueError):
            schedule.build_xml("run", "99:99")
        self.assertIn("T07:05:00", schedule.build_xml("run", "7:05"))

    def test_decode_schtasks_output(self):
        self.assertEqual(schedule._decode("Ошибка".encode()), "Ошибка")
        self.assertEqual(schedule._decode("Ошибка".encode("cp866")), "Ошибка")


class ConnectionPool(unittest.TestCase):
    def test_requests_reuse_connections(self):
        app = server.App()
        self.addCleanup(app.close)
        httpd = server.serve(app, 0)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        opened = []
        real = db.connect

        def counting(path):
            opened.append(path)
            return real(path)

        with mock.patch.object(db, "connect", side_effect=counting):
            for _ in range(10):
                req = urllib.request.Request(url + "/api/kinds", headers={"X-Token": app.token})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.status, 200)
        self.assertLessEqual(len(opened), 1)  # десять запросов подряд — одно соединение из пула
        with urllib.request.urlopen(url + "/favicon.ico", timeout=5) as r:
            self.assertEqual(r.status, 204)  # значок без ключа — не 403 в консоли окна


class KindCache(unittest.TestCase):
    def test_result_is_a_copy(self):
        a = kinds.attrs("Клей плиточный Основит Плитэкс C1 25 кг")
        self.assertEqual(a, {"type": "Клей плиточный", "grade": "C1"})
        a["type"] = "испорчено"
        self.assertEqual(kinds.attrs("Клей плиточный Основит Плитэкс C1 25 кг")["type"], "Клей плиточный")
        self.assertEqual(kinds.attrs(None), {})


class ProductsCatalog(unittest.TestCase):
    """Вкладка «Товары» хранит разобранный каталог — но любая правка и новый сбор видны сразу."""

    def setUp(self):
        self.app = server.App()
        self.addCleanup(self.app.close)
        con = self.app.con
        con.execute("DELETE FROM products")
        run = db.start_run(con)
        for i, name in enumerate(["Штукатурка гипсовая Основит 30 кг", "Клей для плитки Церезит 25 кг"]):
            db.record(
                con,
                run,
                {
                    "key": f"petrovich:{90000 + i}",
                    "site": "petrovich",
                    "name": name,
                    "price": 500.0 + i,
                    "pack_qty": 30.0,
                    "pack_unit": "кг",
                    "source": "feed",
                },
            )
        db.finish_run(con, run, "готово", {})

    def item(self, q="", key="petrovich:90000"):
        return next(p for p in self.app.products(q)["items"] if p["key"] == key)

    def test_search_and_edits_are_visible(self):
        app, con = self.app, self.app.con
        self.assertEqual(app.products("штукатурка гипсовая")["total"], 1)
        self.assertEqual(app.products("ГИПСОВАЯ основит")["total"], 1)  # регистр и порядок слов не важны
        self.assertEqual(app.products("нет такого")["total"], 0)
        self.assertIs(app.products()["items"][0]["is_ours"], True)  # Основит — первым

        db.set_kind(con, "petrovich:90001", "Клей плиточный C2")
        self.assertEqual(self.item(key="petrovich:90001")["kind"], "Клей плиточный C2")
        self.assertEqual(app.products("плиточный c2")["total"], 1)  # и поиск по новому виду

        db.set_pack(con, "petrovich:90000", "25 кг")
        self.assertEqual(self.item()["pack"], "25 кг")
        self.assertEqual(self.item()["per_unit"], 20.0)

        db.decide(con, "petrovich:90000", "exclude")
        self.assertTrue(self.item()["excluded"])
        db.decide(con, "petrovich:90000", "undo")
        self.assertFalse(self.item()["excluded"])

    def test_new_run_is_visible(self):
        con = self.app.con
        self.assertEqual(self.item()["price"], 500.0)
        run = db.start_run(con)
        db.record(
            con,
            run,
            {
                "key": "petrovich:90000",
                "site": "petrovich",
                "name": "Штукатурка гипсовая Основит 30 кг",
                "price": 550.0,
                "pack_qty": 30.0,
                "pack_unit": "кг",
                "source": "feed",
            },
        )
        db.finish_run(con, run, "готово", {})
        self.assertEqual(self.item()["price"], 550.0)
        self.assertTrue(self.item()["in_last_run"])
        self.assertFalse(self.item(key="petrovich:90001")["in_last_run"])


class ExcelLinks(unittest.TestCase):
    def test_long_label_with_quotes_stays_valid(self):
        """Подпись ссылки с кавычками длиннее 255 знаков: Excel не должен «восстанавливать» файл."""
        sh = xlsx.Sheet("Тест", [("Товар", 60, xlsx.LINK)])
        label = 'Штукатурка "Основит" ' * 30 + "\x0b"
        sh.add(xlsx.Link("https://petrovich.ru/product/123456/", label))
        path = os.path.join(tempfile.mkdtemp(prefix="osnovit-xlsx-"), "t.xlsx")
        xlsx.write(path, [sh])
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))
            root = ET.fromstring(z.read(name))  # XML цел
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        formula = next(root.iter(ns + "f")).text
        inner = formula[len("HYPERLINK(") : -1].split('","', 1)[1][:-1]
        self.assertLessEqual(len(inner), 255)
        self.assertEqual(inner.replace('""', "").count('"'), 0)  # кавычки не разрезаны пополам


if __name__ == "__main__":
    unittest.main()
