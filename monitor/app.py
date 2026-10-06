"""Точка входа: окно приложения, сбор по расписанию и служебные команды.

Запустить.cmd                 окно приложения
Запустить.cmd --run           сбор без окна (для Планировщика)
Запустить.cmd --watchdog      проверка «сбор прошёл?» с уведомлением (dead man's switch)
Запустить.cmd --health        проверка здоровья в JSON; код выхода 0 — ок, 1 — предупреждения, 2 — плохо
Запустить.cmd --backup        резервная копия базы и настроек сейчас
Запустить.cmd --restore ФАЙЛ  восстановить базу из копии (приложение должно быть закрыто)
Запустить.cmd --selftest      проверить компьютер: Python, Edge, папки, Планировщик, уведомления, сеть
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser

from . import backup, cdp, collect, config, db, health, log


def run_unattended() -> int:
    """Сбор по расписанию: без человека. Если сайт просит проверку — сеть ставится на паузу, фид работает как обычно."""
    res = collect.run(config.load(), collect.Status(), interactive=False)
    if res.get("skipped"):
        return 0
    return 1 if res.get("error") else 0


def run_health() -> int:
    con = db.connect(config.DB_PATH)
    try:
        h = health.check(con, config.load())
    finally:
        con.close()
    print(json.dumps(h, ensure_ascii=False, indent=1))
    return health.ORDER[h["status"]]


def run_selftest() -> int:
    """Проверка компьютера — понятным текстом, чтобы запускать двойным щелчком или из консоли."""
    from . import selftest

    con = db.connect(config.DB_PATH)
    try:
        r = selftest.run(config.load(), con)
    finally:
        con.close()
    mark = {"ok": "в порядке", "warn": "внимание ", "fail": "ПЛОХО    "}
    print(f"Проверка компьютера — Монитор Основит DIY {config.VERSION}\n")
    for c in r["checks"]:
        print(f"  [{mark[c['status']]}] {c['name']}: {c['detail']}")
        if c.get("hint"):
            print(f"               → {c['hint']}")
    return health.ORDER[r["status"]]


def run_watchdog() -> int:
    con = db.connect(config.DB_PATH)
    try:
        res = health.watchdog(con, config.load())
    finally:
        con.close()
    return health.ORDER[res["status"]]


def run_backup() -> int:
    con = db.connect(config.DB_PATH)
    try:
        path = backup.make(con, "из командной строки")
    finally:
        con.close()
    print(path or "Копия не сделана — см. журнал data/logs/monitor.jsonl")
    return 0 if path else 2


def run_restore(path: str) -> int:
    con = db.connect(config.DB_PATH)
    try:
        if db.lock_info(con, collect.LOCK):
            print("Сейчас идёт сбор — восстановление отменено. Дождитесь окончания и закройте приложение.")
            return 2
    finally:
        con.close()
    try:
        backup.restore(path)
    except (OSError, ValueError, KeyError) as e:
        print(f"Не получилось: {e}")
        return 2
    print("База восстановлена. Текущая сохранена в data/backups/before-restore-….sqlite")
    return 0


def run_window(port: int = 0) -> int:
    from . import server

    app = server.App()
    httpd = server.serve(app, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={app.token}"
    browser = cdp.find_browser(app.settings["edge"].get("path"))
    if browser:
        subprocess.Popen(
            [
                browser,
                f"--app={url}",
                f"--user-data-dir={os.path.join(config.DATA, 'ui')}",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1180,860",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        webbrowser.open(url)
    print(f"Монитор Основит DIY {config.VERSION} открыт: {url}")
    print("Это окно можно свернуть. Закроется само, когда закроете окно приложения.")
    try:
        while True:
            time.sleep(5)
            idle = time.time() - app.last_ping
            if idle > 90 and not app.status.running:
                break
    except KeyboardInterrupt:
        pass
    httpd.shutdown()
    log.info("app.stop", "Окно приложения закрыто")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Монитор цен Основит — Петрович и Лемана ПРО")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run", action="store_true", help="собрать цены без окна (для Планировщика)")
    g.add_argument("--watchdog", action="store_true", help="проверить, что сбор проходит, и уведомить, если нет")
    g.add_argument("--health", action="store_true", help="проверка здоровья в JSON")
    g.add_argument("--backup", action="store_true", help="резервная копия сейчас")
    g.add_argument("--restore", metavar="ФАЙЛ", help="восстановить базу из резервной копии")
    g.add_argument(
        "--selftest", action="store_true", help="проверить компьютер: Python, Edge, папки, Планировщик, сеть"
    )
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    config.ensure_dirs()
    if a.run:
        return run_unattended()
    if a.watchdog:
        return run_watchdog()
    if a.health:
        return run_health()
    if a.backup:
        return run_backup()
    if a.restore:
        return run_restore(a.restore)
    if a.selftest:
        return run_selftest()
    return run_window(a.port)


if __name__ == "__main__":
    sys.exit(main())
