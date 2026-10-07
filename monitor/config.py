"""Папки и настройки. Всё лежит рядом с приложением в папке data — переносится копированием."""

import copy
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "2.8.0"
DATA = os.environ.get("OSNOVIT_DIY_DATA") or os.path.join(ROOT, "data")
FEEDS_DIR = os.path.join(DATA, "feeds")
SAMPLES_DIR = os.path.join(DATA, "samples")
BROWSER_DIR = os.path.join(DATA, "browser")
LOGS_DIR = os.path.join(DATA, "logs")
DB_PATH = os.path.join(DATA, "monitor-2.1.sqlite")  # своё имя: база 2.0.0 рядом не мешает
SETTINGS_PATH = os.path.join(DATA, "settings-2.1.json")

DEFAULTS = {
    "brand_words": ["Основит", "Osnovit"],
    "sites": {
        "petrovich": {
            "enabled": True,
            "feed": "",  # ссылка из Admitad «Товары» или путь к файлу
            "feed_city": "",  # для какого города цены в фиде (из описания программы)
            "edge_enabled": True,  # дособирать в окне Edge то, чего нет в фиде
            "edge_city": "",  # город в окне Edge; заполняется сам, когда человек выбирает город в окне
            "edge_prepare": True,  # сбор из окна приложения: сначала человек открывает сайт и выбирает город
            "search_url": "",  # адрес поиска по сайту с {q}; пусто — поиск сети по умолчанию
        },
        "lemanapro": {
            "enabled": True,
            "feed": "",  # Admitad «Каталог товаров» или «Где Слон?» (YML)
            "feed_city": "",
            "edge_enabled": True,
            "edge_city": "",
            "edge_prepare": True,
            "search_url": "",
        },
    },
    "edge": {
        "pause_s": 12,  # пауза между страницами, как у человека (±30 %)
        "max_pages": 80,  # не больше страниц за один сбор на сеть (каждая — с паузой как у человека)
        "max_section_pages": 30,  # страниц выдачи на один раздел: листаем до конца, но не бесконечно
        "wait_items_s": 10,  # сколько ждать, пока товары дорисуются на странице
        "search_pages": 2,  # страниц выдачи на один поисковый запрос
        "path": "",  # путь к msedge.exe; пусто — найти самому
        "port": 9224,
        "wait_check_s": 25,  # сколько ждать, пока проверка браузера уйдёт сама
        "wait_human_s": 300,  # сколько ждать человека, если не ушла
        "wait_prepare_s": 600,  # сколько ждать, пока человек подготовит окно (город, проверка)
    },
    "feed_interval_h": 6,
    "crosscheck": {
        "n": 6,  # сколько карточек из фида сверять с сайтом за сбор (0 — не сверять)
        "warn_pct": 10,  # расхождение фида с полкой, после которого товар идёт «На проверку»
    },
    "review": {
        "jump_pct": 40,  # скачок цены за один сбор
        "unit_ratio": 3.0,  # цена за кг в N раз выше/ниже середины раздела
        "feed_age_h": 36,  # фид старше — предупреждение
    },
    "analogs": {
        "auto": True,  # после сбора искать товары Основит и их аналоги по всему фиду, а не только в отслеживаемом
        "max_per_kind": 40,  # аналогов на один вид товара (штукатурка гипсовая, затирка…) на сеть
        "types": [],  # какие виды искать; пусто — все, что есть у Основит
        "site_search": True,  # искать аналоги и поиском на сайте сети (окно Edge), если robots.txt разрешает
        "search_queries": 4,  # запросов поиска за сбор на сеть (по кругу: за несколько сборов пройдут все)
        "queries": [],  # свои запросы, по одному: «Кнауф Ротбанд 30 кг», «затирка эпоксидная»
    },
    "report_dir": "",
    "failures": {
        "screenshots": True,  # снимок экрана при сбое (JPEG ~100 КБ, всего не больше 60 дампов / 200 МБ)
    },
    "health": {
        "max_age_h": 26,  # сбор не проходил дольше — тревога (dead man's switch)
        "watchdog": True,  # при включённом расписании — проверка каждые 6 часов
    },
    "alerts": {
        "windows": True,  # всплывающее уведомление Windows
        "webhook_url": "",  # необязательно: POST {"text": …}; если в адресе есть {text} — GET с подстановкой
    },
    "backup": {
        "mirror_dir": "",  # вторая папка для копий: сетевой диск, OneDrive/Яндекс Диск; пусто — не копировать
    },
    "schedule": {
        "at": "08:30",  # время ежедневного сбора
        "wake": False,  # будить компьютер из сна для сбора
    },
    "ui": {
        "expert": False,  # режим специалиста: вкладка «Состояние», метрики, тонкие настройки
    },
}


def _merge(base, extra):
    """Настройки поверх умолчаний. Чужие или испорченные значения (например, от старой версии) пропускаются."""
    out = copy.deepcopy(base)
    if not isinstance(extra, dict):
        return out
    for k, v in extra.items():
        if k not in out:
            continue
        d = out[k]
        if isinstance(d, dict):
            if isinstance(v, dict):
                out[k] = _merge(d, v)
        elif isinstance(d, bool):
            if isinstance(v, bool):
                out[k] = v
        elif isinstance(d, (int, float)):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = v
        elif isinstance(d, list):
            if isinstance(v, list):
                out[k] = [str(x) for x in v]
        elif isinstance(v, str):
            out[k] = v
    return out


def ensure_dirs():
    for d in (DATA, FEEDS_DIR, SAMPLES_DIR, BROWSER_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)


def load():
    ensure_dirs()
    data = {}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    return _merge(DEFAULTS, data)


def save(settings):
    ensure_dirs()
    clean = _merge(DEFAULTS, settings)
    e = clean["edge"]
    e["pause_s"] = max(5, float(e.get("pause_s") or 12))  # быстрее человека не ходим
    e["max_pages"] = max(1, min(200, int(e.get("max_pages") or 40)))
    e["max_section_pages"] = max(1, min(100, int(e.get("max_section_pages") or 30)))
    clean["feed_interval_h"] = max(1, float(clean.get("feed_interval_h") or 6))
    cc = clean["crosscheck"]
    cc["n"] = max(0, min(30, int(cc.get("n") or 0)))
    cc["warn_pct"] = max(1, float(cc.get("warn_pct") or 10))
    clean["health"]["max_age_h"] = max(2, float(clean["health"].get("max_age_h") or 26))
    an = clean["analogs"]
    an["max_per_kind"] = max(5, min(300, int(an.get("max_per_kind") or 40)))
    an["search_queries"] = max(0, min(20, int(an.get("search_queries") or 0)))
    an["queries"] = [" ".join(q.split()) for q in an.get("queries") or [] if q.strip()][:50]
    e["search_pages"] = max(1, min(10, int(e.get("search_pages") or 2)))
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    if os.path.exists(SETTINGS_PATH):
        shutil.copyfile(SETTINGS_PATH, SETTINGS_PATH + ".bak")  # прошлая версия настроек — на случай ошибки
    os.replace(tmp, SETTINGS_PATH)
    return clean


def default_report_dir():
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home, "Монитор Основит DIY")


def report_dir(settings):
    d = settings.get("report_dir") or default_report_dir()
    os.makedirs(d, exist_ok=True)
    return d
