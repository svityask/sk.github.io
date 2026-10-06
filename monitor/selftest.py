"""Проверка компьютера: всё ли есть для сбора, особенно по расписанию.

Каждая проверка — {name, status: ok/warn/fail, detail, hint}. Сеть трогаем бережно: один запрос к каждому фиду
(HEAD, без скачивания) и к robots.txt сайта, если включено окно сбора.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

from . import backup, cdp, config, schedule, sites

TIMEOUT = 15


def _writable(path: str) -> str | None:
    """None — можно писать; иначе текст ошибки."""
    try:
        os.makedirs(path, exist_ok=True)
        fd, p = tempfile.mkstemp(prefix=".probe-", dir=path)
        os.close(fd)
        os.remove(p)
        return None
    except OSError as e:
        return str(e)


def _probe(url: str) -> tuple[str, str]:
    from .feeds import USER_AGENT

    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return "ok", f"отвечает ({r.status})"
    except urllib.error.HTTPError as e:
        if e.code in (405, 501):  # сервер не умеет HEAD — но отвечает
            return "ok", f"отвечает ({e.code})"
        if e.code in (401, 403):
            return "warn", f"доступ закрыт ({e.code}) — ссылка устарела или программа не одобрена"
        if e.code == 404:
            return "fail", "404 — ссылка неверная"
        if e.code == 429:
            return "warn", "просит ходить реже (429) — это нормально, сбор подождёт"
        return "warn", f"ответ {e.code}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return "fail", f"не отвечает: {reason}"


def run(settings: dict | None = None, con: sqlite3.Connection | None = None, network: bool = True) -> dict[str, Any]:
    settings = settings or config.load()
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "hint": hint})

    # Python
    v = sys.version_info
    # не commonpath: на Windows он падает, если Python и приложение на разных дисках (C: и D:)
    exe, root = (os.path.normcase(os.path.abspath(x)) for x in (sys.executable, config.ROOT))
    portable = exe.startswith(root.rstrip("\\/") + os.sep)
    where = "переносной, в папке приложения" if portable else sys.executable
    if v >= (3, 10):
        add("Python", "ok", f"{v.major}.{v.minor}.{v.micro} ({where})")
    else:
        add("Python", "fail", f"{v.major}.{v.minor} — нужен 3.10 или новее", "Скопируйте папку python из версии 2.0.0")

    # папки
    err = _writable(config.DATA)
    add(
        "Папка данных",
        "fail" if err else "ok",
        err or config.DATA,
        "Приложение должно лежать в папке, куда можно писать (не в Program Files)" if err else "",
    )
    try:
        rd = config.report_dir(settings)
        err = _writable(rd)
        add("Папка отчётов", "fail" if err else "ok", err or rd)
    except OSError as e:
        add("Папка отчётов", "fail", str(e), "Укажите другую папку в Настройках")
    mdir = backup.mirror_folder(settings)
    if mdir:
        err = _writable(mdir)
        add(
            "Вторая папка копий",
            "warn" if err else "ok",
            err or mdir,
            "Сетевой диск не подключён? Копии всё равно делаются в папку приложения" if err else "",
        )
    try:
        free = shutil.disk_usage(config.DATA).free
        add(
            "Место на диске",
            "ok" if free > 1e9 else ("warn" if free > 200e6 else "fail"),
            f"свободно {free / 1e9:.1f} ГБ",
        )
    except OSError:
        pass

    # база
    if con is not None:
        q = backup.quick_check(con)
        size = os.path.getsize(config.DB_PATH) if os.path.exists(config.DB_PATH) else 0
        add("База данных", "ok" if q == "ok" else "fail", f"целостность: {q}, {size / 1e6:.1f} МБ")

    # Edge
    edge_on = any(c.get("enabled") and c.get("edge_enabled") for c in settings["sites"].values())
    b = cdp.find_browser(settings["edge"].get("path") or "")
    if b:
        add("Microsoft Edge", "ok", b)
    else:
        add(
            "Microsoft Edge",
            "warn" if edge_on else "ok",
            "не найден" if edge_on else "не найден (окно сбора выключено — не нужен)",
            "Настройки → включите «Режим специалиста» → «Путь к Microsoft Edge»" if edge_on else "",
        )

    # Windows: Планировщик и уведомления
    if sys.platform.startswith("win"):
        st = schedule.state()
        if not st.get("available"):
            add("Планировщик Windows", "warn", "недоступен", "Возможно, его запрещает политика компьютера")
        elif st.get("enabled"):
            add(
                "Планировщик Windows",
                "ok",
                "сбор по расписанию включён" + (", проверка — тоже" if st.get("watchdog") else ", проверка выключена"),
            )
        else:
            add("Планировщик Windows", "warn", "расписание не включено", "Настройки → Сбор по расписанию")
        ps = shutil.which("powershell") or shutil.which("powershell.exe")
        add(
            "Уведомления Windows",
            "ok" if ps else "warn",
            "PowerShell есть" if ps else "PowerShell не найден — уведомления не придут",
            "" if ps else "Настройки → «Режим специалиста» → webhook (например, Telegram)",
        )
    else:
        add("Планировщик Windows", "warn", "это не Windows — расписание недоступно")

    # сеть
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    if proxies:
        add("Прокси", "ok", "системный прокси: " + ", ".join(sorted(set(proxies.values())))[:200])
    if network:
        for site, conf in settings["sites"].items():
            if not conf.get("enabled"):
                continue
            title = sites.SITES[site]["title"]
            feed = (conf.get("feed") or "").strip()
            if feed.lower().startswith(("http://", "https://")):
                status, detail = _probe(feed)
                add(f"Фид: {title}", status, detail)
            elif feed:
                ok = os.path.exists(feed)
                add(f"Фид: {title}", "ok" if ok else "fail", "файл есть" if ok else f"файл не найден: {feed}")
            else:
                add(f"Фид: {title}", "warn", "ссылка не задана", "Настройки → ссылка на фид из Admitad или «Где Слон?»")
            if conf.get("edge_enabled"):
                status, detail = _probe(sites.origin(sites.SITES[site]["home"]) + "/robots.txt")
                add(f"Сайт: {title}", status, detail)

    order = {"ok": 0, "warn": 1, "fail": 2}
    worst = max((c["status"] for c in checks), key=lambda s: order[s], default="ok")
    return {"status": worst, "checks": checks, "ts": time.time(), "version": config.VERSION}
