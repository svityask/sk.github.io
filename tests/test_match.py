"""Стратегия «полка → карточки»: сопоставление товара с полки с фидом и решение о заходе в карточку."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "OSNOVIT_DIY_DATA" not in os.environ:
    os.environ["OSNOVIT_DIY_DATA"] = tempfile.mkdtemp(prefix="osnovit-match-")

from monitor import extract, match  # noqa: E402

BRAND = ["Основит", "Osnovit"]


def feed_item(code, name, price, vendor=""):
    return {"key": f"lemanapro:{code}", "code": code, "name": name, "price": price, "vendor": vendor, "source": "feed"}


FOUND = {
    o["key"]: o
    for o in (
        feed_item("11111111", "Клей плиточный Основит Плитэкс C1 25 кг", 400, "Основит"),
        feed_item("22222222", "Клей плиточный Церезит CM11 25 кг", 450),
        feed_item("82065432", "Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг", 590, "Основит"),
        feed_item("82065433", "Штукатурка гипсовая Основит Гипсвелл PC21 G 5 кг", 150, "Основит"),
    )
}
FOUND["lemanapro:edge1"] = {"name": "Не из фида", "price": 1, "source": "edge"}


class Matching(unittest.TestCase):
    def setUp(self):
        self.index = match.FeedIndex("lemanapro", FOUND)

    def test_by_code_in_url(self):
        it = {"url": "https://lemanapro.ru/product/kley-cerezit-22222222/", "name": "что угодно"}
        self.assertEqual(self.index.match(it), ("lemanapro:22222222", "артикул"))

    def test_by_name_any_order_and_case(self):
        it = {"url": "https://lemanapro.ru/product/bez-koda/", "name": "клей ПЛИТОЧНЫЙ Церезит CM11, 25 кг"}
        self.assertEqual(self.index.match(it), ("lemanapro:22222222", "название"))

    def test_truncated_name_needs_same_pack(self):
        it = {"url": "", "name": "Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг"}
        self.assertEqual(self.index.match(it)[0], "lemanapro:82065432")
        cut = {"url": "", "name": "Штукатурка гипсовая Основит Гипсв…"}
        self.assertEqual(self.index.match(cut), (None, None))  # подходит и 30 кг, и 5 кг — не угадываем
        cut30 = {"url": "", "name": "Штукатурка Основит Гипсвелл 30 кг"}
        self.assertEqual(self.index.match(cut30), ("lemanapro:82065432", "название и фасовка"))

    def test_own_code_not_in_feed_is_another_product(self):
        """Тот же текст названия, но на полке свой артикул, которого нет в фиде, — другой товар, не склеиваем."""
        f = dict(FOUND["lemanapro:22222222"], url="https://lemanapro.ru/product/kley-cerezit-22222222/")
        index = match.FeedIndex("lemanapro", {"lemanapro:22222222": f})
        other = {"url": "https://lemanapro.ru/product/kley-cerezit-seryy-99999999/", "name": f["name"]}
        self.assertEqual(index.match(other), (None, None))
        no_code = {"url": "", "name": f["name"]}
        self.assertEqual(index.match(no_code)[0], "lemanapro:22222222")
        # в фиде нет адреса товара (ключ по id предложения) — сравнить артикулы нельзя, сопоставляем по названию
        g = dict(f, url="")
        index = match.FeedIndex("lemanapro", {"lemanapro:22222222": g})
        self.assertEqual(index.match(other)[0], "lemanapro:22222222")

    def test_edge_items_are_not_feed(self):
        self.assertNotIn("lemanapro:edge1", self.index.items)
        self.assertEqual(self.index.match({"name": "Не из фида"}), (None, None))

    def test_tokens(self):
        self.assertEqual(match.tokens("Шпаклёвка 0,9 л"), frozenset({"шпаклевка", "0.9", "л"}))
        self.assertEqual(match.tokens("Штукатурка Гипсв…"), frozenset({"штукатурка"}))
        self.assertTrue(match.truncated("Клей ..."))


class DeepRules(unittest.TestCase):
    def reasons(self, item, feed=None, warn=10):
        return match.deep_reasons(item, feed, warn, BRAND)

    def test_price_matches_feed_no_card(self):
        f = FOUND["lemanapro:22222222"]
        self.assertEqual(self.reasons({"name": f["name"], "price": 455}, f), [])  # 1 % — не идём

    def test_price_gap(self):
        f = FOUND["lemanapro:22222222"]
        self.assertEqual(self.reasons({"name": f["name"], "price": 520}, f), [match.PRICE_GAP])
        self.assertEqual(self.reasons({"name": f["name"], "price": 520}, f, warn=20), [])  # порог из настроек

    def test_price_from_is_not_compared(self):
        f = FOUND["lemanapro:22222222"]
        r = self.reasons({"name": f["name"], "price": 300, "price_from": True}, f)
        self.assertEqual(r, [match.PRICE_FROM])  # «от 300» — не расхождение с фидом

    def test_pack_unknown(self):
        self.assertEqual(self.reasons({"name": "Клей плиточный Волма Керамик", "price": 300}), [match.PACK])
        # фасовка есть в фиде — с полки её узнавать не нужно
        f = feed_item("5", "Клей плиточный Волма Керамик 25 кг", 300)
        self.assertEqual(self.reasons({"name": "Клей плиточный Волма Керамик", "price": 300}, f), [])

    def test_kind_only_when_needed(self):
        # вид не распознан: товар Основит — нужен для аналогов
        self.assertIn(match.KIND, self.reasons({"name": "Основит Неизвестное 25 кг", "price": 1}))
        # название обрезано — карточка покажет полное
        self.assertIn(match.KIND, self.reasons({"name": "Смесь Волма Универ…", "price": 1}))
        # чужой товар, название полное, вид просто не наш — карточка не поможет
        self.assertNotIn(match.KIND, self.reasons({"name": "Ведро строительное 12 л", "price": 1}))

    def test_order_ours_first_then_importance(self):
        a = ({"name": "Клей Церезит"}, [match.PACK])
        b = ({"name": "Клей Волма"}, [match.PRICE_GAP])
        c = ({"name": "Клей Основит"}, [match.KIND])
        self.assertEqual(
            [t[0]["name"] for t in match.order([a, b, c], BRAND)], ["Клей Основит", "Клей Волма", "Клей Церезит"]
        )


class CardMerge(unittest.TestCase):
    def test_card_refines_shelf_item(self):
        it = {"name": "Клей Волма Кера…", "price": 300, "price_from": True, "code": None, "available": None}
        card = {
            "name": "Клей плиточный Волма Керамик 25 кг",
            "price": 310,
            "old_price": None,
            "code": "33333333",
            "available": True,
            "params": {"Вес, кг": ("25", "")},
        }
        match.merge_card(it, card)
        self.assertEqual(it["price"], 310)
        self.assertFalse(it["price_from"])
        self.assertEqual(it["name"], "Клей плиточный Волма Керамик 25 кг")
        self.assertEqual(it["code"], "33333333")
        self.assertEqual(it["params"], {"Вес, кг": ("25", "")})
        self.assertTrue(it["available"])


class PageParsing(unittest.TestCase):
    def test_price_from_in_layout_and_data(self):
        page = {
            "url": "https://lemanapro.ru/catalogue/x/",
            "cards": [
                {"url": "https://lemanapro.ru/product/a-11111111/", "name": "Клей", "price": "300", "from": True},
                {"url": "https://lemanapro.ru/product/b-22222222/", "name": "Клей Б", "price": "400"},
            ],
        }
        items, _ = extract.products_from_page(page, [], "lemanapro")
        by = {i["code"]: i for i in items}
        self.assertTrue(by["11111111"]["price_from"])
        self.assertFalse(by["22222222"]["price_from"])
        data = {
            "products": [{"name": "Клей Церезит CM11", "productLmCode": "12345678", "priceFrom": 450, "price": 450}]
        }
        items, _ = extract.products_from_page({"url": "https://lemanapro.ru/"}, [("u", data)], "lemanapro")
        self.assertTrue(items[0]["price_from"])

    def test_card_specs_and_jsonld_weight(self):
        self.assertEqual(extract.page_params({"specs": {"Вес, кг": "25"}}), {"Вес, кг": ("25", "")})
        ld = [
            {
                "@type": "Product",
                "name": "Клей",
                "sku": "33333333",
                "weight": {"value": "25", "unitCode": "KGM"},
                "additionalProperty": [{"name": "Основа", "value": "цементная"}],
                "offers": {"price": "310"},
            }
        ]
        items, _ = extract.products_from_page(
            {"url": "https://lemanapro.ru/product/k-33333333/", "jsonld": ld}, [], "lemanapro"
        )
        self.assertEqual(items[0]["params"], {"Основа": ("цементная", ""), "Вес": ("25", "кг")})


if __name__ == "__main__":
    unittest.main()
