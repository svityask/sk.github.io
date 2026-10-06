"""Отчёт Excel по итогам сбора."""

import os
import time
from datetime import datetime

from . import config, db, sites, spot, units
from .analysis import SOURCE_TITLE
from .xlsx import DATE, INT, LINK, MONEY, PCT, TEXT, WRAP, Link, Sheet, write


def _site(s):
    return sites.SITES.get(s, {}).get("title", s)


def _dt(ts):
    return datetime.fromtimestamp(ts) if ts else None


def _avail(v):
    return {1: "есть", 0: "нет"}.get(v, "") if v is not None else ""


def build(settings, con, run_id, products, market, review, stats, summary, groups=None):
    groups = groups or {t["id"]: (t["title"] or t["ref"]) for t in db.tracked(con)}

    ch = Sheet(
        "Изменения цен",
        [
            ("Сеть", 13, TEXT),
            ("Товар", 60, LINK),
            ("Было, ₽", 11, MONEY),
            ("Стало, ₽", 11, MONEY),
            ("Изменение", 11, PCT),
            ("Источник", 12, TEXT),
            ("Основит", 9, TEXT),
            ("Словами", 90, WRAP),
        ],
    )
    ch.color_sign(4)
    changes = sorted(
        summary.get("changes", []),
        key=lambda c: (not c["is_ours"], -abs((c["new"] - c["old"]) / c["old"]) if c["old"] else 0),
    )
    for c in changes:
        ch.add(
            _site(c["site"]),
            Link(c["url"], c["name"]),
            c["old"],
            c["new"],
            (c["new"] - c["old"]) / c["old"] if c["old"] else None,
            SOURCE_TITLE.get(c["source"], c["source"]),
            "да" if c["is_ours"] else "",
            c["text"],
        )
    if not changes:
        ch.add("", "Изменений цен с прошлого сбора нет")

    mk = Sheet(
        "Основит и рынок",
        [
            ("Сеть", 13, TEXT),
            ("Вид товара", 26, TEXT),
            ("Товар Основит", 50, LINK),
            ("Фасовка", 9, TEXT),
            ("Цена, ₽", 10, MONEY),
            ("За ед., ₽", 10, MONEY),
            ("Ед.", 5, TEXT),
            ("С чем сравнили", 14, TEXT),
            ("Середина за ед., ₽", 12, MONEY),
            ("Мы относительно середины", 13, PCT),
            ("Конкурентов дороже нас", 12, TEXT),
            ("Самый дешёвый конкурент", 45, LINK),
            ("Его цена за ед., ₽", 11, MONEY),
            ("Наличие", 9, TEXT),
            ("Источник", 11, TEXT),
        ],
    )
    mk.color_sign(9)
    level_title = {"аналоги": "аналоги", "тот же тип": "тот же тип", "раздел": "весь раздел"}
    for r in sorted(market, key=lambda r: (r["site"], r["kind"] or "я", -(r["deviation"] or 0))):
        lvl = level_title.get(r["level"], "нет сравнения")
        if r["level"] and r["count"] < 3:
            lvl += f" (мало: {r['count']})"
        mk.add(
            _site(r["site"]),
            (r["kind"] or "не распознан") + (" ✎" if r.get("kind_manual") else ""),
            Link(r["url"], r["name"]),
            r["pack"],
            r["price"],
            r["per_unit"],
            r["unit"],
            lvl,
            r["median"],
            r["deviation"] / 100 if r["deviation"] is not None else None,
            f"{r['cheaper_than']} из {r['count']}" if r["cheaper_than"] is not None else "—",
            Link(r["min_url"], r["min_name"]) if r["min_name"] else "",
            r["min"],
            _avail(r["available"]),
            r["source"],
        )
    if not market:
        mk.add(
            "",
            "Товаров Основит в отслеживаемых разделах не нашлось. Добавьте раздел, где стоит Основит, "
            "или проверьте слова бренда в настройках.",
        )

    rv = Sheet(
        "На проверку",
        [
            ("Сеть", 13, TEXT),
            ("Товар — откройте ссылку", 60, LINK),
            ("Цена, ₽", 10, MONEY),
            ("Почему на проверке", 80, WRAP),
        ],
    )
    for it in sorted(review, key=lambda i: (i["site"], i["kind"], i["name"] or "")):
        rv.add(_site(it["site"]), Link(it["url"], it["name"]), it["price"], it["reason"])
    if not review:
        rv.add("", "Всё распознано, проверять нечего")

    asm = Sheet(
        "Ассортимент",
        [
            ("Что случилось", 18, TEXT),
            ("Сеть", 13, TEXT),
            ("Товар", 60, LINK),
            ("Раздел", 24, TEXT),
            ("Цена, ₽", 10, MONEY),
            ("Основит", 9, TEXT),
        ],
    )
    ev_title = {"new": "Появился в фиде", "gone": "Пропал из фида", "out": "Закончился", "back": "Снова в наличии"}
    order = {"gone": 0, "out": 1, "new": 2, "back": 3}
    events = sorted(
        summary.get("assortment", []),
        key=lambda e: (not e.get("is_ours"), order.get(e["type"], 9), e["site"], e.get("name") or ""),
    )
    for e in events:
        asm.add(
            ev_title.get(e["type"], e["type"]),
            _site(e["site"]),
            Link(e["url"], e["name"]),
            groups.get(e.get("group_id"), ""),
            e.get("price"),
            "да" if e.get("is_ours") else "",
        )
    if not events:
        asm.add("", "", "С прошлого сбора ассортимент и наличие не менялись (или это первый сбор)")

    cc_rows = []
    for site, info in summary.get("sites", {}).items():
        for it in (info.get("crosscheck") or {}).get("items", []):
            cc_rows.append((site, it))
    cc = Sheet(
        "Сверка фида",
        [
            ("Сеть", 13, TEXT),
            ("Товар", 60, LINK),
            ("В фиде, ₽", 11, MONEY),
            ("На сайте, ₽", 11, MONEY),
            ("Сайт относительно фида", 13, PCT),
        ],
    )
    cc.color_sign(4)
    for site, it in cc_rows:
        cc.add(_site(site), Link(it["url"], it["name"]), it["feed"], it["site"], it["pct"] / 100)
    if not cc_rows:
        cc.add("", "Сверки не было: нужен фид и включённый сбор с сайта в настройках сети")

    dyn, dyn_data = _dynamics(con, market)

    al = Sheet(
        "Все цены",
        [
            ("Сеть", 13, TEXT),
            ("Раздел", 24, TEXT),
            ("Товар", 60, LINK),
            ("Бренд", 14, TEXT),
            ("Фасовка", 9, TEXT),
            ("Цена, ₽", 10, MONEY),
            ("Старая цена, ₽", 11, MONEY),
            ("Скидка", 8, PCT),
            ("За ед., ₽", 10, MONEY),
            ("Ед.", 5, TEXT),
            ("Наличие", 9, TEXT),
            ("Источник", 11, TEXT),
            ("Город", 15, TEXT),
            ("Основит", 8, TEXT),
            ("Артикул", 12, TEXT),
        ],
    )
    for p in sorted(
        products, key=lambda p: (p["site"], groups.get(p["group_id"], ""), not p.get("is_ours"), p["name"] or "")
    ):
        disc = -(p["old_price"] - p["price"]) / p["old_price"] if p.get("old_price") else None
        al.add(
            _site(p["site"]),
            groups.get(p["group_id"], ""),
            Link(p["url"], p["name"]),
            p.get("vendor") or "",
            units.pack_label(p.get("pack_qty"), p.get("pack_unit")),
            p["price"],
            p.get("old_price"),
            disc,
            units.per_unit(p["price"], p.get("pack_qty")),
            p.get("pack_unit") or "",
            _avail(p.get("available")),
            SOURCE_TITLE.get(p["source"], p["source"]),
            p.get("city") or "",
            "да" if p.get("is_ours") else "",
            p.get("code") or "",
        )

    hs = Sheet(
        "История",
        [
            ("Когда", 16, DATE),
            ("Сеть", 13, TEXT),
            ("Товар", 60, LINK),
            ("Цена, ₽", 10, MONEY),
            ("Старая цена, ₽", 11, MONEY),
            ("Наличие", 9, TEXT),
            ("Источник", 11, TEXT),
            ("Город", 15, TEXT),
        ],
    )
    for h in db.history_since(con, time.time() - 90 * 86400):
        hs.add(
            _dt(h["ts"]),
            _site(h["site"]),
            Link(h["url"], h["name"]),
            h["price"],
            h.get("old_price"),
            _avail(h.get("available")),
            SOURCE_TITLE.get(h["source"], h["source"]),
            h.get("city") or "",
        )

    sm = Sheet("Сводка", [("Что", 34, TEXT), ("Значение", 90, WRAP)])
    sm.add("Сбор", datetime.now().strftime("%d.%m.%Y %H:%M"))
    sm.add("Версия приложения", config.VERSION)
    sm.add("Цен в сборе", (summary.get("prices", len(products)), INT))
    sm.add("Изменений цен", (len(changes), INT))
    sm.add("На проверку", (len(review), INT))
    st = spot.stats(con)
    sm.add(
        "Выборочная проверка",
        f"верно {st['accuracy']} % из {st['total']} карточек за 90 дней" if st["total"] else "ещё не проводилась",
    )
    for site, info in summary.get("sites", {}).items():
        t = info.get("title", _site(site))
        if info.get("note"):
            sm.add(t, info["note"])
            continue
        f = info.get("feed")
        if f is None:
            sm.add(f"{t} — фид", "не задан (основной путь не настроен)")
        elif f.get("error"):
            sm.add(f"{t} — фид", "ошибка: " + f["error"])
        else:
            when = (
                f"выгрузка сети от {_dt(f['date']).strftime('%d.%m.%Y %H:%M')} ({f.get('age_h')} ч назад)"
                if f.get("date")
                else "дата выгрузки в файле не указана"
            )
            sm.add(
                f"{t} — фид",
                f"{f.get('status')}; {when}; "
                f"формат {f.get('format')}; в файле {f.get('total')} позиций, взято {f.get('taken')}",
            )
        e = info.get("edge")
        if e is None:
            sm.add(f"{t} — сайт (Edge)", "не понадобился")
        elif e.get("error"):
            sm.add(f"{t} — сайт (Edge)", "ошибка: " + e["error"])
        else:
            text = (
                f"страниц {e.get('pages')}, цен {e.get('taken')}, прочитано: {', '.join(e.get('via') or []) or '—'}, "
                f"город на сайте: {e.get('city') or 'не прочитан'}"
            )
            if e.get("stopped"):
                text += f"; остановлено: {e['stopped']}"
            sm.add(f"{t} — сайт (Edge)", text)
            for n in e.get("notes") or []:
                sm.add("", n)
        c = info.get("crosscheck")
        if c:
            sign = "дороже" if c["avg_pct"] > 0 else "дешевле"
            sm.add(
                f"{t} — сверка фида",
                f"{c['n']} карточек открыто на сайте; совпали {c['same']}; "
                f"в среднем сайт {sign} фида на {abs(c['avg_pct'])} % "
                f"(по модулю {c['avg_abs_pct']} %); расхождение 5 % и больше — {c['n'] and c['over5']}",
            )
    for w in summary.get("warnings", []):
        sm.add("Внимание", w)
    sm.add("", "")
    sm.add("Рынок по видам товаров", "")
    for s in stats:
        med = f"{s['median']:.2f}".replace(".", ",") if s["median"] else "—"
        mn = f"{s['min']:.2f}".replace(".", ",") if s["min"] else "—"
        sm.add(
            f"{_site(s['site'])}: {s['kind']}",
            f"середина {med} ₽/{s['unit']}, минимум {mn} ₽/{s['unit']}, конкурентов {s['count']}",
        )
    sm.add("", "")
    sm.add(
        "Откуда цены",
        "Фид — каталог, который сеть сама выкладывает для партнёров (Admitad, «Где Слон?»). "
        "Сайт (Edge) — запасной путь: только разделы и товары из списка, с паузами, по правилам "
        "robots.txt, проверку браузера проходит человек.",
    )

    return save([ch, mk, asm, rv, dyn, cc, al, hs, dyn_data, sm], settings, summary)


def _folder(settings, summary):
    """Папка отчётов из настроек; недоступна (сетевой диск, флешка) — «Документы», с предупреждением."""
    try:
        return config.report_dir(settings)
    except OSError as e:
        fallback = config.default_report_dir()
        os.makedirs(fallback, exist_ok=True)
        summary.setdefault("warnings", []).append(
            f"Папка отчётов недоступна ({settings.get('report_dir')}: {e.strerror or e}) — отчёт положен в {fallback}"
        )
        return fallback


def save(sheets, settings, summary):
    """Пишет отчёт во временный файл и только потом даёт ему имя.

    Так оборванная запись не оставит битый .xlsx, а отчёт с тем же именем (два сбора за минуту, файл открыт
    в Excel) не перезаписывается и не мешает: новому отчёту достаётся имя с номером «(2)», «(3)»…
    """
    folder = _folder(settings, summary)
    # strftime не получает кириллицу: на Windows с Python 3.10 это UnicodeEncodeError ('locale' codec)
    base = f"Цены Петрович и Лемана ПРО {datetime.now():%Y-%m-%d %H%M}"
    tmp = os.path.join(folder, f"~{base}.{os.getpid()}.part")
    try:
        write(tmp, sheets)
        for n in range(1, 100):
            path = os.path.join(folder, f"{base}.xlsx" if n == 1 else f"{base} ({n}).xlsx")
            if os.path.exists(path):
                continue
            try:
                os.rename(tmp, path)  # не replace: в Windows rename не затрёт файл, появившийся только что
                return path
            except FileExistsError:
                continue
        raise OSError(f"в папке {folder} слишком много отчётов с именем «{base}»")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


OURS_COLOR, MARKET_COLOR = "2A78D6", "EB6834"  # слоты 1 и 2 проверенной палитры; середина — пунктиром


def _dynamics(con, market, days=180, limit=12):
    """Лист с графиками «Основит против середины аналогов» и лист с данными для них."""
    data = Sheet("Динамика — данные", [("Сбор", 16, TEXT)])
    charts = Sheet("Динамика", [("", 9, TEXT)] * 16)
    charts.filter = False
    charts.header = False
    charts.add(
        "Цена Основит за кг/л (сплошная линия) и середина аналогов (пунктир). Числа — на листе «Динамика — данные»."
    )
    data.filter = False
    keys = [r["key"] for r in market if r.get("per_unit") and r.get("median")][:limit]
    if not keys:
        charts.rows = [("Графики появятся, когда у товаров Основит будет с чем сравнить",)]
        return charts, data
    points = [p for p in db.market_points_since(con, time.time() - days * 86400) if p["key"] in keys]
    runs = sorted({p["run_id"]: p["ts"] for p in points}.items(), key=lambda kv: kv[1])
    row_of = {run_id: i for i, (run_id, _) in enumerate(runs)}
    names: dict[str, tuple[str, str, str]] = {}
    for p in points:
        names.setdefault(p["key"], (p["name"], p["site"], p.get("unit") or ""))
    cols = []
    for k in keys:
        if k not in names:
            continue
        name, site, unit = names[k]
        short = name if len(name) <= 48 else name[:47] + "…"
        data.columns += [(f"{short} — Основит, ₽/{unit}", 16, MONEY), (f"{short} — середина, ₽/{unit}", 16, MONEY)]
        cols.append((k, short, site, unit))
    table = [[datetime.fromtimestamp(ts).strftime("%d.%m.%y %H:%M")] + [None] * (2 * len(cols)) for _, ts in runs]
    col_of = {k: 1 + 2 * i for i, (k, *_) in enumerate(cols)}
    for p in points:
        if p["key"] in col_of:
            r = table[row_of[p["run_id"]]]
            r[col_of[p["key"]]] = p.get("per_unit")
            r[col_of[p["key"]] + 1] = p.get("median")
    for r in table:
        data.add(*r)
    last = len(table) + 1
    for i, (k, short, site, unit) in enumerate(cols):
        c = col_of[k]
        charts.add_chart(
            f"{short} · {_site(site)}",
            (data.name, 0, 2, last),
            [
                ("Основит", data.name, c, 2, last, OURS_COLOR),
                ("Середина аналогов", data.name, c + 1, 2, last, MARKET_COLOR),
            ],
            anchor=((i % 2) * 8, 2 + (i // 2) * 17, 8, 16),
            y_title=f"₽/{unit}",
        )
    return charts, data
