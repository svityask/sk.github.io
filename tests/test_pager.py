"""Листание по схеме адресов сайта (monitor/pager.py) — без браузера."""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-pager-")

from monitor import config, pager  # noqa: E402


class Petrovich(unittest.TestCase):
    def scheme(self, url):
        p = pager.for_url("petrovich", url)
        self.assertIsNotNone(p)
        return p

    def test_n_minus_one_and_sort_kept(self):
        p = self.scheme("https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc")
        self.assertEqual(p.build_url(1), "https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc")
        self.assertEqual(p.build_url(2), "https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc&p=1")
        self.assertEqual(p.build_url(3), "https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc&p=2")
        self.assertEqual(p.build_url(4), "https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc&p=3")

    def test_no_sort_is_not_invented(self):
        p = self.scheme("https://petrovich.ru/catalog/1547/")
        self.assertEqual(p.build_url(1), "https://petrovich.ru/catalog/1547/")
        self.assertEqual(p.build_url(2), "https://petrovich.ru/catalog/1547/?p=1")

    def test_page_param_in_pasted_url_is_dropped(self):
        """Вставили адрес 5-й страницы — 1-я всё равно без p, остальные считаются заново."""
        p = self.scheme("https://spb.petrovich.ru/catalog/1547/?p=4&sort=price_asc#top")
        self.assertEqual(p.build_url(1), "https://spb.petrovich.ru/catalog/1547/?sort=price_asc")
        self.assertEqual(p.build_url(2), "https://spb.petrovich.ru/catalog/1547/?sort=price_asc&p=1")

    def test_landed(self):
        p = self.scheme("https://petrovich.ru/catalog/1547/")
        self.assertTrue(p.landed("https://petrovich.ru/catalog/1547/?p=1", 2))
        self.assertFalse(p.landed("https://petrovich.ru/catalog/1547/", 2))  # перенаправил на 1-ю
        self.assertFalse(p.landed("https://petrovich.ru/catalog/9999/?p=1", 2))  # в другой раздел

    def test_only_section_addresses(self):
        self.assertIsNone(pager.for_url("petrovich", "https://petrovich.ru/catalog/1547/700001/"))  # карточка
        self.assertIsNone(pager.for_url("petrovich", "https://petrovich.ru/search/?q=затирка"))  # поиск
        self.assertIsNone(pager.for_url("petrovich", "https://lemanapro.ru/catalog/1547/"))  # чужой сайт


class SectionAddress(unittest.TestCase):
    def test_sort_and_filters_kept_tracking_dropped(self):
        from monitor import sites

        self.assertEqual(
            sites.normalize_section_url(
                "https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc&utm_source=ya"
            ),
            "https://moscow.petrovich.ru/catalog/285396726/?sort=review_desc",
        )
        self.assertEqual(
            sites.normalize_section_url("www.leroymerlin.ru/catalogue/shtukaturki?yclid=1&brand=Osnovit#top"),
            "https://lemanapro.ru/catalogue/shtukaturki/?brand=Osnovit",
        )


class Lemana(unittest.TestCase):
    def test_shift_ids_required(self):
        p = pager.for_url("lemanapro", "https://lemanapro.ru/catalogue/shtukaturki/")
        self.assertEqual(p.build_url(1), "https://lemanapro.ru/catalogue/shtukaturki/")
        self.assertFalse(p.ready_for(2))
        with self.assertRaises(ValueError):
            p.build_url(2)  # без shiftIds — та же выдача, такой адрес не строим
        p.extra_params["shiftIds"] = "c2hpZnQ6MTIz"
        self.assertEqual(p.build_url(2), "https://lemanapro.ru/catalogue/shtukaturki/?page=2&shiftIds=c2hpZnQ6MTIz")
        self.assertEqual(p.build_url(3), "https://lemanapro.ru/catalogue/shtukaturki/?page=3&shiftIds=c2hpZnQ6MTIz")

    def test_old_page_and_shift_in_url_dropped(self):
        p = pager.for_url("lemanapro", "https://lemanapro.ru/catalogue/shtukaturki/?page=4&shiftIds=old&sort=price")
        self.assertEqual(p.build_url(1), "https://lemanapro.ru/catalogue/shtukaturki/?sort=price")


class Settings(unittest.TestCase):
    def test_yaml(self):
        data = pager.parse_yaml(
            '# комментарий\npetrovich:\n  page_param: "p"   # параметр\n  page_offset: -1\n  first_page_has_param: false\n'
        )
        self.assertEqual(data, {"petrovich": {"page_param": "p", "page_offset": -1, "first_page_has_param": False}})
        with self.assertRaises(ValueError):
            pager.parse_yaml("  page_param: p\n")  # ключ без сайта
        with self.assertRaises(ValueError):
            pager.parse_yaml("petrovich\n")

    def test_shipped_file_matches_spec(self):
        with open(pager.CONFIG_PATH, encoding="utf-8") as f:
            data = pager.parse_yaml(f.read())
        self.assertEqual((data["petrovich"]["page_param"], data["petrovich"]["page_offset"]), ("p", -1))
        self.assertEqual((data["lemanapro"]["page_param"], data["lemanapro"]["page_offset"]), ("page", 0))
        self.assertTrue(data["lemanapro"]["requires_shift_ids"])

    def test_user_override_and_broken_file(self):
        d = tempfile.mkdtemp(prefix="osnovit-sites-")
        user = os.path.join(d, "sites.yaml")
        with open(user, "w", encoding="utf-8") as f:
            f.write("petrovich:\n  page_param: page\n  page_offset: 0\n")
        with mock.patch.object(pager, "USER_PATH", user):
            p = pager.for_url("petrovich", "https://petrovich.ru/catalog/1547/")
            self.assertEqual(p.build_url(2), "https://petrovich.ru/catalog/1547/?page=2")
        with open(user, "w", encoding="utf-8") as f:
            f.write("сломано\n")
        with mock.patch.object(pager, "USER_PATH", user):
            p = pager.for_url("petrovich", "https://petrovich.ru/catalog/1547/")
            self.assertEqual(p.build_url(2), "https://petrovich.ru/catalog/1547/?p=1")  # испорченный файл пропущен

    def test_pause_and_limits(self):
        self.assertEqual(config.pause_range(config.load()), (10.0, 13.0))
        self.assertEqual(config.pause_range({"pause_min_s": 2, "pause_max_s": 1}), (5.0, 5.0))
        old = {"edge": {"max_section_pages": 30, "max_pages": 80, "pause_s": 12}}
        config._upgrade(old)
        self.assertEqual(old["edge"], {"pause_s": 12})  # старые лимиты по умолчанию — на новые
        mine = {"edge": {"max_section_pages": 50}}
        config._upgrade(mine)
        self.assertEqual(mine["edge"], {"max_section_pages": 50})  # своё значение не трогаем


class Log(unittest.TestCase):
    def test_signature_and_lines(self):
        a = [{"name": f"Товар {i}"} for i in range(8)]
        self.assertEqual(pager.signature(a), pager.signature([*a[:5], {"name": "другой"}]))  # первые 5
        self.assertNotEqual(pager.signature(a), pager.signature(a[1:]))
        lines = []
        with mock.patch.object(pager.log, "info", lambda ev, msg="", **f: lines.append((ev, msg, f))):
            s = pager.SectionLog("Штукатурки", "petrovich.ru")
            s.page(1, 25, "url_scheme", 12)
            s.page(2, 25, "url_scheme", 12)
            s.stop(pager.NO_NEW_ITEMS)
            s.stop(pager.PAGE_LIMIT)  # вторая причина не пишется
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1][1], "Категория «Штукатурки», страница 2/12, найдено 25 товаров (всего 50)")
        f = lines[1][2]
        self.assertEqual(
            (f["category"], f["site"], f["page"], f["found"], f["total_found"], f["method"], f["status"]),
            ("Штукатурки", "petrovich.ru", 2, 25, 50, "url_scheme", "ok"),
        )
        self.assertEqual((lines[2][2]["stop_reason"], lines[2][2]["last_page"]), ("no_new_items", 2))


if __name__ == "__main__":
    unittest.main()
