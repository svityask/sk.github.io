"""Поддельный сайт сети для проверки окна сбора в настоящем браузере.

Отвечает по https на 127.0.0.1:<порт>; браузер запускается с --host-resolver-rules="MAP lemanapro.ru 127.0.0.1:<порт>",
поэтому код приложения ходит на «lemanapro.ru», как в жизни. Страницы:
  /robots.txt               — запрещает /catalogue/zapret/
  /catalogue/shtukaturki/   — две страницы выдачи: товары в вёрстке + данные, которые страница берёт fetch'ем
  /product/shpaklevka…      — карточка с разметкой schema.org
  /catalogue/limit/         — 429 с Retry-After
  /catalogue/captcha/       — 403 «подтвердите, что вы не робот» (не уходит сама)
  /catalogue/check/         — проверка браузера, которая проходит сама через 6 с (Edge на CI открывает страницу дольше 3 с)
  /                         — главная с городом в шапке (окно готовит человек)
  /catalogue/smesi/         — выдача для стратегии «полка → карточки»: совпадает с фидом, расходится с фидом,
                              цена «от», без фасовки, без артикула в ссылке; карточки — /product/… из CARDS
"""

from __future__ import annotations

import http.server
import json
import os
import ssl
import threading
from urllib.parse import parse_qs, urlsplit

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

API = {
    1: {
        "content": [
            {
                "productLmCode": "82065432",
                "name": "Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг",
                "productLink": "/product/shtukaturka-osnovit-82065432/",
                "price": {"displayMain": 585, "displayOld": 640, "cardPrice": 560},
                "brand": "Основит",
            },
            {
                "productLmCode": "12345678",
                "name": "Штукатурка гипсовая Knauf Ротбанд 30 кг",
                "productLink": "/product/shtukaturka-knauf-12345678/",
                "price": {"displayMain": 715},
                "brand": "Knauf",
            },
        ]
    },
    2: {
        "content": [
            {
                "productLmCode": "12345690",
                "name": "Штукатурка гипсовая Старатели 30 кг",
                "productLink": "/product/starateli-12345690/",
                "price": {"displayMain": 505},
                "brand": "Старатели",
            }
        ]
    },
}


def _head(city):
    return f'<header><div class="header-region">Ваш город: {city}</div></header>'


def _section(page, city):
    nxt = '<a rel="next" href="/catalogue/shtukaturki/?page=2">Следующая</a>' if page == 1 else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Штукатурки — Лемана ПРО</title></head><body>
{_head(city)}<div id="list"></div>
<div class="card"><a href="/product/shtukaturka-volma-sloy-30-kg-12345679/">Штукатурка Волма Слой 30 кг</a>
 <div><span class="price">495 ₽</span></div></div>
<div class="card"><a href="/product/shtukaturka-unis-teplon-25-kg-12345680/">Штукатурка Юнис Теплон 25 кг</a>
 <div><span style="text-decoration:line-through">520 ₽</span> <span class="price">455 ₽</span>
 <span class="card-price">440 ₽</span></div></div>
{nxt}
<script>fetch('/api/catalog?page={page}').then(r=>r.json()).then(d=>{{document.getElementById('list').textContent=d.content.length;}});</script>
</body></html>"""


PRODUCT = """<!doctype html><html><head><meta charset="utf-8"><title>Шпаклёвка Основит</title>
<script type="application/ld+json">{"@type":"Product","name":"Шпаклёвка финишная Основит Эконсилк PG34 W 20 кг","sku":"66666666",
"brand":{"@type":"Brand","name":"Основит"},"offers":{"@type":"Offer","price":"605","priceCurrency":"RUB",
"availability":"https://schema.org/InStock"}}</script></head><body>HEAD<h1>Шпаклёвка финишная Основит</h1><span>605 ₽</span>
</body></html>"""

CHECK = """<!doctype html><html><head><meta charset="utf-8"><title>Проверка браузера</title></head><body>Проверяем ваш браузер…
<script>setTimeout(()=>{document.title='Штукатурки';document.body.innerHTML='<header>Ваш город: Москва</header>'+
'<div class=c><a href="/product/x-12345699/">Штукатурка тест 30 кг</a><span>333 ₽</span></div>';},6000)</script></body></html>"""

CAPTCHA = """<!doctype html><html><head><meta charset="utf-8"><title>Проверка браузера</title></head><body>
<h2>Подтвердите, что вы не робот</h2><div style="width:300px;height:80px;border:2px solid #999">[ капча ]</div></body></html>"""


SMESI = [  # (адрес, название в выдаче, цена в выдаче)
    ("/product/kley-osnovit-pliteks-11111111/", "Клей плиточный Основит Плитэкс C1 25 кг", "400 ₽"),
    ("/product/kley-cerezit-cm11-22222222/", "Клей плиточный Церезит CM11 25 кг", "520 ₽"),
    ("/product/kley-volma-keramik-33333333/", "Клей плиточный Волма Керамик", "от 300 ₽"),
    ("/product/grunt-unis-44444444/", "Грунтовка глубокого проникновения Юнис", "250 ₽"),
    ("/product/shtukaturka-osnovit-bez-koda/", "Штукатурка гипсовая Основит Гипсвелл PC21 G 30 кг", "590 ₽"),
]

CARDS = {  # карточки: название, цена, характеристики
    "/product/kley-cerezit-cm11-22222222/": ("Клей плиточный Церезит CM11 25 кг", 520, {}),
    "/product/kley-volma-keramik-33333333/": ("Клей плиточный Волма Керамик 25 кг", 310, {"Вес, кг": "25"}),
    "/product/grunt-unis-44444444/": ("Грунтовка глубокого проникновения Юнис", 250, {"Объём, л": "10"}),
}


def _smesi(city):
    cards = "".join(
        f'<div class="card"><a href="{u}">{n}</a><div><span class="price">{p}</span></div></div>' for u, n, p in SMESI
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Смеси — Лемана ПРО</title></head><body>
{_head(city)}{cards}</body></html>"""


def _card(path, city):
    name, price, specs = CARDS[path]
    code = path.rstrip("/").rsplit("-", 1)[-1]
    rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in specs.items())
    ld = json.dumps(
        {"@type": "Product", "name": name, "sku": code, "offers": {"@type": "Offer", "price": str(price)}},
        ensure_ascii=False,
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{name}</title>
<script type="application/ld+json">{ld}</script></head><body>{_head(city)}<h1>{name}</h1><span>{price} ₽</span>
<table>{rows}</table></body></html>"""


# Листание выдачи — как на настоящих сайтах (по снимкам экрана):
#   /catalog/shtukaturki/ (Петрович) — номера «1 2 3» и «Дальше» без rel="next", по 2 товара, ?p=N;
#   /catalogue/pokazat/   (Лемана)  — кнопка «Показать ещё» без ссылки: 2 товара + ещё по 2 за нажатие (2 раза);
#   /catalogue/lenivo/              — товары появляются через 3 с после загрузки и ещё два — после прокрутки.
def _pager(page, total):
    nums = "".join(
        f'<span class="active">{i}</span>' if i == page else f'<a href="/catalog/shtukaturki/?p={i}">{i}</a>'
        for i in range(1, total + 1)
    )
    nxt = f'<a href="/catalog/shtukaturki/?p={page + 1}"><span>Дальше</span> <svg></svg></a>' if page < total else ""
    return f'<div class="pagination">{nums} {nxt}</div>'


def _petrovich_page(page, city):
    cards = "".join(
        f'<div class="card"><a href="/catalog/1234/{700000 + page * 10 + i}/">Штукатурка гипсовая Марка{page}{i} 30 кг</a>'
        f"<div>Основа: Гипсовая</div><div>Вес, кг: 30</div><span>{400 + page * 10 + i} ₽</span></div>"
        for i in range(2)
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Штукатурки — Петрович</title></head><body>
{_head(city)}{cards}{_pager(page, 3)}<a href="/catalog/shtukaturki/?tags=1">Показать все…</a></body></html>"""


_POKAZAT = """<!doctype html><html><head><meta charset="utf-8"><title>Смеси — Лемана ПРО</title></head><body>HEAD
<div id="list"></div><button id="more">Показать ещё</button>
<script>
let page = 0;
function add() {
  for (let i = 0; i < 2; i++) {
    const n = 55500000 + page * 10 + i;
    const d = document.createElement('div'); d.className = 'card';
    d.innerHTML = '<a href="/product/smes-' + n + '/">Затирка цементная Марка' + n + ' 2 кг</a><span>' + (100 + page) + ' ₽</span>';
    document.getElementById('list').appendChild(d);
  }
  page++;
  if (page >= 3) document.getElementById('more').remove();
}
add();
document.getElementById('more').onclick = () => setTimeout(add, 800);
</script></body></html>"""

_LENIVO = """<!doctype html><html><head><meta charset="utf-8"><title>Клеи — Лемана ПРО</title></head><body>HEAD
<div id="list" style="min-height:3000px"></div>
<script>
function card(n, p) {
  const d = document.createElement('div'); d.className = 'card';
  d.innerHTML = '<a href="/product/kley-' + n + '/">Клей плиточный Марка' + n + ' 25 кг</a><span>' + p + ' ₽</span>';
  document.getElementById('list').appendChild(d);
}
setTimeout(() => { card(66600001, 300); card(66600002, 310); }, 3000);
let more = false;
window.addEventListener('scroll', () => { if (!more && window.scrollY > 200) { more = true; setTimeout(() => card(66600003, 320), 300); } });
</script></body></html>"""


class FakeSite:
    def __init__(self, city="Москва"):
        self.city = city
        self.log: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, body, ctype="text/html; charset=utf-8", code=200, headers=None):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                if u.path != "/favicon.ico":
                    outer.log.append(self.path)
                p = u.path
                if p == "/robots.txt":
                    return self.send("User-agent: *\nDisallow: /catalogue/zapret/\n", "text/plain; charset=utf-8")
                if p == "/api/catalog":
                    page = int(q.get("page", ["1"])[0])
                    return self.send(json.dumps(API[page], ensure_ascii=False), "application/json")
                if p == "/catalogue/shtukaturki/":
                    return self.send(_section(int(q.get("page", ["1"])[0]), outer.city))
                if p == "/":
                    return self.send(f"<!doctype html><title>Лемана ПРО</title>{_head(outer.city)}<h1>Главная</h1>")
                if p == "/catalog/shtukaturki/":
                    return self.send(_petrovich_page(int(q.get("p", ["1"])[0]), outer.city))
                if p == "/catalogue/pokazat/":
                    return self.send(_POKAZAT.replace("HEAD", _head(outer.city)))
                if p == "/catalogue/lenivo/":
                    return self.send(_LENIVO.replace("HEAD", _head(outer.city)))
                if p == "/catalogue/smesi/":
                    return self.send(_smesi(outer.city))
                if p in CARDS:
                    return self.send(_card(p, outer.city))
                if p.startswith("/product/shpaklevka"):
                    return self.send(PRODUCT.replace("HEAD", _head(outer.city)))
                if p == "/catalogue/check/":
                    return self.send(CHECK)
                if p == "/catalogue/captcha/":
                    return self.send(CAPTCHA, code=403)
                if p == "/catalogue/limit/":
                    return self.send("<title>429</title>Too Many Requests", code=429, headers={"Retry-After": "5400"})
                return self.send("<h1>404</h1>", code=404)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(os.path.join(FIX, "fake-site-cert.pem"), os.path.join(FIX, "fake-site-key.pem"))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def browser_args(self):
        return [
            f"--host-resolver-rules=MAP lemanapro.ru 127.0.0.1:{self.port}, MAP petrovich.ru 127.0.0.1:{self.port}",
            "--ignore-certificate-errors",
            "--no-proxy-server",
        ]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
