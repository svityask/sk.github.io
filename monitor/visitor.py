"""Запасной путь: сбор в окне Edge «как посетитель».

Правила, которые здесь нельзя выключить:
  • только разделы и товары из списка «Что отслеживаем», не весь каталог;
  • robots.txt сети соблюдается: запрещённый адрес не открывается;
  • пауза между страницами не меньше 5 секунд (по умолчанию 12 ± 30 %), лимит страниц за сбор;
  • никакого распознавания капчи, прокси, подмены браузера; проверку проходит человек сам;
  • 429 или капча без человека → сеть сразу ставится на паузу не меньше чем на час (guard.Blocked);
  • страница не открылась — один повтор с нарастающей паузой; сайт дважды отказал — сеть до конца сбора не трогаем.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import subprocess
import time
import urllib.robotparser
from typing import Any

from . import cdp, config, dumps, extract, guard, log, match, sites
from .metrics import Meter

MAX_SAMPLES = 40


class Stop(Exception):
    """Сеть остановлена до конца сбора (причина — в тексте). failed=True — это сбой источника."""

    def __init__(self, msg: str, failed: bool = False):
        super().__init__(msg)
        self.failed = failed


class Visitor:
    def __init__(self, settings, status, interactive=True):
        self.s = settings["edge"]
        self.settings = settings
        self.status = status
        self.interactive = interactive
        self._tab: cdp.Tab | None = None
        self.proc = None
        self.pages = 0
        self.robots = {}
        self._last_open = 0.0
        self.meter = Meter("", "edge")
        self.site = ""

    # ------------------------------------------------------------ окно

    def open(self):
        path = cdp.find_browser(self.s.get("path"))
        self.proc = cdp.launch(
            path,
            config.BROWSER_DIR,
            int(self.s.get("port") or 9224),
            offscreen=True,
            extra_args=self.s.get("extra_args") or (),
        )
        self._tab = cdp.Tab(int(self.s.get("port") or 9224))

    @property
    def tab(self) -> cdp.Tab:
        if self._tab is None:
            raise cdp.BrowserError("Окно сбора не открыто")
        return self._tab

    @property
    def is_open(self) -> bool:
        return self._tab is not None

    def close(self):
        """Закрывает вкладку сбора, а окно Edge — если его запустили мы.

        Окно, которое уже было открыто (например, человек выбирал в нём город), не трогаем. Запущенное нами
        окно стоит за краем экрана: если его не закрыть, оно так и висело бы невидимым после сбора
        по расписанию, с открытым портом отладки.
        """
        tab, self._tab = self._tab, None
        if self.proc is not None and tab is not None:
            try:
                tab.send("Browser.close", timeout=10)  # штатно: профиль (город, cookies) сохранится
            except (cdp.BrowserError, OSError):
                pass
        if tab is not None:
            tab.close()
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.info("browser.closed", "Окно сбора закрыто")

    # ------------------------------------------------------------ шаги

    def _sleep(self, seconds):
        while seconds > 0:
            if self.status.cancelled():
                raise Stop("сбор остановлен вами")
            step = min(seconds, 0.5)
            time.sleep(step)
            seconds -= step

    def _pause(self):
        base = max(5.0, float(self.s.get("pause_s") or 12))
        self._sleep(base * random.uniform(0.7, 1.3) - (time.time() - self._last_open))

    def _open(self, url):
        """Открывает адрес с паузой как у человека. Не открылась — один повтор через 10–20 с."""

        def attempt():
            if self.pages >= int(self.s.get("max_pages") or 40):
                raise Stop(f"достигнут лимит {self.pages} страниц за сбор — остальное в следующий раз")
            self._pause()
            self.pages += 1
            self.meter.pages += 1
            self.meter.requests += 1
            self._last_open = time.time()
            t0 = time.time()
            status = self.tab.navigate(url)
            log.debug(
                "page.open", f"Открыта страница ({status})", url=url, status=status, ms=round((time.time() - t0) * 1000)
            )
            return status

        def on_retry(n, e, delay):
            self.meter.retries += 1
            log.warning("page.retry", f"Страница не открылась ({e}), повтор через {delay:.0f} с", url=url)

        return guard.retry(
            attempt,
            attempts=int(self.s.get("open_attempts") or 2),
            base=12.0,
            retry_on=(cdp.BrowserError,),
            sleep=self._sleep,
            on_retry=on_retry,
            cancelled=self.status.cancelled,
        )

    def allowed(self, url):
        """robots.txt: можно ли открыть адрес. Не прочитался — открываем только отслеживаемое (как и так)."""
        org = sites.origin(url)
        if org not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            text = ""
            try:
                status = self._open(org + "/robots.txt")
                if status == 429:
                    self._blocked_429(org + "/robots.txt", status)
                text = self.tab.evaluate("document.body ? document.body.innerText : ''") or ""
                if status and status >= 400:
                    text = ""
            except cdp.BrowserError:
                text = ""
            if "user-agent" in text.lower():
                rp.parse(text.splitlines())
                self.robots[org] = rp
            else:
                self.robots[org] = None
            log.info("robots.read", "robots.txt прочитан" if self.robots[org] else "robots.txt не прочитан", url=org)
        rp = self.robots[org]
        ok = True if rp is None else rp.can_fetch("*", url)
        if not ok:
            log.info("robots.disallow", "robots.txt запрещает адрес — не открываем", url=url)
        return ok

    def _shot(self):
        if not self.settings.get("failures", {}).get("screenshots", True):
            return None
        try:
            return self.tab.send("Page.captureScreenshot", timeout=20, format="jpeg", quality=55).get("data")
        except cdp.BrowserError:
            return None

    def _dump(self, reason, url, status=None, page=None):
        html = None
        try:
            html = self.tab.html()
        except cdp.BrowserError:
            pass
        dumps.save(
            self.site,
            reason,
            url=url,
            status=status,
            title=(page or {}).get("title", ""),
            html=html,
            screenshot_b64=self._shot(),
        )

    def _blocked_429(self, url, status):
        self.meter.http_429 += 1
        retry_after = guard.parse_retry_after(self.tab.last_headers.get("retry-after"))
        self._dump("429", url, status)
        raise guard.Blocked("429", retry_after, "сайт ответил 429 — слишком много запросов")

    def _read(self, url, site, expect_city):
        status = self._open(url)
        if status == 429:
            self._blocked_429(url, status)
        page = self.tab.evaluate(extract.PAGE_SCRIPT, timeout=40) or {}
        if extract.is_check_page(page, status):
            self.meter.captcha += 1
            log.warning("page.check", "Сайт показал проверку браузера или отказ", url=url, status=status)
            page, status = self._wait_check(url, site, status)
        city = extract.city_of(page) or sites.city_from_url(url)
        if expect_city and city and not extract.same_city(expect_city, city):
            raise Stop(
                f"в окне сбора выбран город «{city}», а в настройках — «{expect_city}». "
                f"Ничего не записано, чтобы не смешать цены двух городов"
            )
        network = self.tab.json_responses(sites.SITES[site]["data_hosts"])
        items, via = extract.products_from_page(page, network, site)
        log.debug("page.parsed", f"Товаров на странице: {len(items)}", url=url, items=len(items), via=via)
        if items and via == "вёрстка":
            self._sample(site, page, via)
        return page, items, via, city

    def _wait_check(self, url, site, status):
        """Проверка браузера: ждём, пока уйдёт сама; не ушла — зовём человека. Сами не проходим никогда."""
        deadline = time.time() + float(self.s.get("wait_check_s") or 25)
        page: dict[str, Any] = {}
        while time.time() < deadline:
            self.tab.pump(3)
            page = self.tab.evaluate(extract.PAGE_SCRIPT, timeout=40) or {}
            if not extract.is_check_page(page):
                log.info("page.check_cleared", "Проверка браузера прошла сама", url=url)
                return page, 200
        reason = "denied" if status in (401, 403, 503) else "captcha"
        if not self.interactive:
            self._dump(reason, url, status, page)
            raise guard.Blocked(reason, None, "сайт показал проверку браузера, а сбор идёт без человека")
        title = sites.SITES[site]["title"]
        self.tab.show_window(True)
        ok = self.status.ask_human(
            f"{title}: сайт просит подтвердить, что вы человек. Пройдите проверку в окне Edge и нажмите «Готово».",
            lambda: not extract.is_check_page(self.tab.evaluate(extract.PAGE_SCRIPT, timeout=40) or {}),
            float(self.s.get("wait_human_s") or 300),
        )
        self.tab.show_window(False)
        page = self.tab.evaluate(extract.PAGE_SCRIPT, timeout=40) or {}
        if not ok or extract.is_check_page(page):
            self._dump(reason, url, status, page)
            raise guard.Blocked(reason, None, "проверку в окне не прошли")
        log.info("page.check_human", "Проверку прошёл человек", url=url)
        return page, 200

    def prepare(self, site, expect_city=""):
        """Окно готовит человек: открывает сайт, выбирает город, если надо — проходит проверку, жмёт «Готово».

        Так сбор идёт в обычной сессии посетителя с выбранным им городом. Приложение ничего не обходит:
        проверку проходит человек, дальше — те же паузы, лимит страниц и robots.txt.
        Возвращает город, выбранный в окне (или None, если прочитать его не удалось).
        """
        title = sites.SITES[site]["title"]
        home = sites.SITES[site]["home"]
        self.site = site
        if self.allowed(home):
            self._open(home)
        self.tab.show_window(True)
        ask = f"{title}: окно сбора открыто. Выберите в нём город (и магазин); если сайт попросит — пройдите проверку. "
        try:
            for attempt in range(2):
                ok = self.status.ask_human(
                    ask + "Потом нажмите «Готово» — дальше приложение соберёт само.",
                    lambda: False,  # «Готово» — только от человека: когда город выбран, знает он
                    float(self.s.get("wait_prepare_s") or 600),
                )
                if not ok:
                    raise Stop("окно сбора не подготовили — сайт пропущен в этот раз")
                page = self.tab.evaluate(extract.PAGE_SCRIPT, timeout=40) or {}
                if extract.is_check_page(page):
                    ask = f"{title}: сайт всё ещё показывает проверку. Пройдите её в окне. "
                    continue
                city = extract.city_of(page) or sites.city_from_url(page.get("url") or "")
                if expect_city and city and not extract.same_city(expect_city, city) and attempt == 0:
                    ask = (
                        f"{title}: в окне выбран город «{city}», а цены фида — для «{expect_city}». "
                        f"Выберите «{expect_city}», чтобы не смешать цены двух городов. "
                    )
                    continue
                log.info("window.prepared", f"Окно подготовлено, город: {city or 'не прочитан'}", city=city)
                return city
            raise Stop("проверку в окне не прошли — сайт пропущен в этот раз")
        finally:
            self.tab.show_window(False)

    def _sample(self, site, page, via):
        """Образец страницы, прочитанной только по вёрстке, — чтобы перевести разбор на данные (без снимка)."""
        try:
            os.makedirs(config.SAMPLES_DIR, exist_ok=True)
            name = time.strftime(f"{site}-%Y%m%d-%H%M%S-{self.pages}")
            with gzip.open(os.path.join(config.SAMPLES_DIR, name + ".json.gz"), "wt", encoding="utf-8") as f:
                json.dump({"via": via, "page": {k: v for k, v in page.items() if k != "state"}}, f, ensure_ascii=False)
            with gzip.open(os.path.join(config.SAMPLES_DIR, name + ".html.gz"), "wt", encoding="utf-8") as f:
                f.write(self.tab.html())
            files = sorted(
                (os.path.join(config.SAMPLES_DIR, n) for n in os.listdir(config.SAMPLES_DIR)),
                key=os.path.getmtime,
                reverse=True,
            )
            for old in files[MAX_SAMPLES * 2 :]:
                os.remove(old)
        except (OSError, cdp.BrowserError):
            pass

    # ------------------------------------------------------------ сеть целиком

    def run_site(self, site, sections, products, expect_city="", checks=(), checks_n=0, meter=None, deep=None):
        """sections: [(group_id, url, title)], products: [(group_id, url, code)], checks: [(key, url, code)].

        Порядок: shallow — страницы разделов; карточки отслеживаемых товаров, которых нет в выдаче; deep —
        карточки товаров с полки, которые отобрал deep(items) -> [(товар, [причины])] (см. match.py);
        сверка фида с полкой. Все шаги — с одним лимитом страниц и теми же паузами.

        Возвращает {'items', 'checks', 'notes', 'stopped', 'failed', 'blocked', 'pages', 'via', 'city',
        'shallow', 'deep'}. blocked = {'reason', 'retry_after', 'detail'} — источник попросил остановиться.
        """
        self.site = site
        self.meter = meter or Meter(site, "edge")
        res: dict[str, Any] = {
            "items": [],
            "checks": [],
            "notes": [],
            "stopped": None,
            "failed": False,
            "blocked": None,
            "pages": 0,
            "via": set(),
            "city": None,
            "skipped": 0,
            "refusals": 0,
            "shallow": 0,  # товаров с полки (из выдачи разделов)
            "deep": [],  # [{'url', 'reasons', 'ok'}] — открытые по решению карточки
        }
        start_pages = self.pages
        title = sites.SITES[site]["title"]
        try:
            for gid, url, sec_title in sections:
                url = sites.normalize_url(url)
                if (
                    expect_city
                    and sites.city_from_url(url)
                    and not extract.same_city(expect_city, sites.city_from_url(url))
                ):
                    res["notes"].append(
                        f"Раздел «{sec_title}» — адрес другого города ({sites.city_from_url(url)}), пропущен"
                    )
                    continue
                visited = set()
                for n in range(int(self.s.get("max_section_pages") or 5)):
                    if not self.allowed(url):
                        res["notes"].append(f"robots.txt запрещает {url} — не открываем")
                        res["skipped"] += 1
                        break
                    self.status.set(f"{title}: раздел «{sec_title}», страница {n + 1}")
                    try:
                        page, items, via, city = self._read(url, site, expect_city)
                    except cdp.BrowserError as e:
                        self._refused(res, url, e)
                        break
                    res["city"] = res["city"] or city
                    res["via"].add(via)
                    if not items:
                        self.meter.empty += 1
                        self._dump("empty", url, None, page)
                    for it in items:
                        it["group_id"] = gid
                    res["items"] += items
                    visited.add(url)
                    nxt = (page.get("next") or "").split("#")[0]
                    if not items or not nxt or nxt in visited or sites.site_of(nxt) != site:
                        break
                    url = nxt  # без normalize: номер страницы — в параметрах адреса
            res["shallow"] = len(res["items"])
            # пустой артикул или адрес не считается «уже найден» — иначе пропускались бы все карточки без артикула
            got = {v for it in res["items"] for v in (it.get("code"), it.get("url")) if v}
            for gid, url, code in products:
                url = sites.normalize_url(url)
                if (code or sites.code_from_url(url)) in got or url in got:
                    continue  # уже нашёлся в разделе — лишнюю страницу не открываем
                it = self._card(site, url, code, expect_city, res, f"{title}: карточка {code or url}")
                if it:
                    it["group_id"] = gid
                    res["items"].append(it)
                    got |= {v for v in (it.get("code"), it.get("url")) if v}  # тот же товар второй раз не открываем
            # deep: карточки товаров с полки — только тех, где без карточки не обойтись
            for it, reasons in deep(res["items"][: res["shallow"]]) if deep else []:
                url = it.get("url")
                if not url:
                    continue
                card = self._card(
                    site, url, it.get("code"), expect_city, res, f"{title}: уточняю ({', '.join(reasons)}) {url}"
                )
                if card:
                    match.merge_card(it, card)
                    it["deep"] = reasons
                res["deep"].append({"url": url, "reasons": reasons, "ok": bool(card)})
            # сверка фида с полкой: checks_n карточек из фида, кандидатов с запасом; страниц — не больше 2×checks_n
            tries = 0
            for key, url, code in checks:
                if len(res["checks"]) >= checks_n or tries >= 2 * checks_n:
                    break
                url = sites.normalize_url(url)
                if (code or sites.code_from_url(url)) in got or url in got:
                    continue  # цена с сайта для него уже есть
                tries += 1
                it = self._card(site, url, code, expect_city, res, f"{title}: сверка с сайтом, {code or url}")
                if it:
                    it["check_key"] = key
                    res["checks"].append(it)
                    got |= {v for v in (it.get("code"), it.get("url")) if v}
        except Stop as e:
            res["stopped"] = str(e)
            res["failed"] = e.failed
        except guard.Blocked as b:
            res["blocked"] = {"reason": b.reason, "retry_after": b.retry_after, "detail": b.detail}
            res["stopped"] = b.detail or b.reason
        res["pages"] = self.pages - start_pages
        self.meter.prices = len(res["items"]) + len(res["checks"])
        return res

    def _refused(self, res, url, e):
        self.meter.errors += 1
        res["refusals"] += 1
        res["notes"].append(f"{url}: {e}")
        log.warning("page.failed", f"Страница не открылась: {e}", url=url)
        self._dump("open-failed", url)
        if res["refusals"] >= 2:
            raise Stop("сайт дважды не открылся — сеть пропущена до следующего сбора", failed=True)

    def _card(self, site, url, code, expect_city, res, status_text):
        """Одна карточка товара → товар с ценой или None (причина — в res['notes'])."""
        if expect_city and sites.city_from_url(url) and not extract.same_city(expect_city, sites.city_from_url(url)):
            res["notes"].append(f"{url} — адрес другого города, пропущен")
            return None
        if not self.allowed(url):
            res["notes"].append(f"robots.txt запрещает {url} — не открываем")
            res["skipped"] += 1
            return None
        self.status.set(status_text)
        try:
            page, items, _via, city = self._read(url, site, expect_city)
        except cdp.BrowserError as e:
            self._refused(res, url, e)
            return None
        res["city"] = res["city"] or city
        want = code or sites.code_from_url(url)
        pick = (
            [i for i in items if want and i.get("code") == want]
            or [i for i in items if i.get("url") == url]
            or [i for i in items if i.get("via") == "разметка"][:1]
        )
        if not pick:
            self.meter.empty += 1
            res["notes"].append(f"На карточке не нашлась цена: {url}")
            self._dump("no-price", url, None, page)
            return None
        it = dict(pick[0])
        it["url"] = it.get("url") or url
        it["code"] = it.get("code") or want
        it["params"] = {**extract.page_params(page), **(it.get("params") or {})}  # характеристики — для фасовки
        res["via"].add(it["via"])
        return it
