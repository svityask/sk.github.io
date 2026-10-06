"""Проверка здоровья и dead man's switch.

check()     — набор проверок: давно ли был удачный сбор, есть ли цены по каждой сети, свежесть фида, источники на
              паузе, целостность базы, место на диске, возраст резервной копии, зависший сбор.
watchdog()  — запускается Планировщиком Windows каждые 6 часов независимо от сбора. Если сбор не проходил дольше
              max_age_h (или здоровье «плохо»), шлёт уведомление: всплывающее окно Windows и, если задан, webhook.
              Одна и та же тревога повторяется не чаще раза в сутки; когда всё починилось — одно сообщение «в порядке».
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

from . import backup, config, db, guard, log, sites

ORDER = {"ok": 0, "warn": 1, "fail": 2}
STATE_FILE = "alert-state.json"
REPEAT_S = 24 * 3600


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "никогда"
    h = (now - ts) / 3600
    return f"{h:.0f} ч назад" if h >= 1 else f"{max(1, round(h * 60))} мин назад"


def check(con: sqlite3.Connection, settings: dict | None = None, now: float | None = None) -> dict[str, Any]:
    settings = settings or config.load()
    now = now or time.time()
    max_age = float(settings.get("health", {}).get("max_age_h") or 26) * 3600
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    tracked = con.execute("SELECT site, COUNT(*) n FROM tracked GROUP BY site").fetchall()
    tracked_sites = {r["site"] for r in tracked}
    ok_run = con.execute("SELECT id, finished FROM runs WHERE status='готово' ORDER BY id DESC LIMIT 1").fetchone()
    last = con.execute("SELECT id, status, started, finished, summary FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    # 1. удачный сбор
    if not tracked_sites:
        add("Сбор", "warn", "Ничего не отслеживается — сбор не настроен")
    elif not ok_run:
        add("Сбор", "warn", "Удачных сборов ещё не было")
    elif now - (ok_run["finished"] or 0) > max_age:
        add("Сбор", "fail", f"Последний удачный сбор {_ago(ok_run['finished'], now)} — дольше {max_age / 3600:.0f} ч")
    else:
        add("Сбор", "ok", f"Последний удачный сбор {_ago(ok_run['finished'], now)}")

    # 2. последний сбор
    if last and last["status"] in ("ошибка", "прерван"):
        detail = ""
        try:
            detail = (json.loads(last["summary"] or "{}").get("error") or "")[:200]
        except ValueError:
            pass
        add("Последний сбор", "warn", f"№{last['id']} — {last['status']}" + (f": {detail}" if detail else ""))
    running = db.lock_info(con, "collect")
    if last and last["status"] == "идёт" and not running:
        add("Последний сбор", "warn", f"№{last['id']} завис: помечен «идёт», но никто его не выполняет")

    # 3. цены по каждой сети
    for site, conf in settings.get("sites", {}).items():
        if not conf.get("enabled") or site not in tracked_sites:
            continue
        title = sites.SITES[site]["title"]
        r = con.execute("SELECT MAX(last_seen) t FROM products WHERE site=?", (site,)).fetchone()
        t = r["t"] if r else None
        if not t:
            add(f"Цены: {title}", "warn" if not ok_run else "fail", "Цен по сети ещё нет")
        elif now - t > max_age:
            add(f"Цены: {title}", "fail", f"Последняя цена {_ago(t, now)}")
        else:
            add(f"Цены: {title}", "ok", f"Последняя цена {_ago(t, now)}")
        meta_p = os.path.join(config.FEEDS_DIR, f"{site}.feed.json")
        if conf.get("feed") and os.path.exists(meta_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    meta = json.load(f)
                age = now - float(meta.get("fetched_at") or 0)
                if meta.get("stale"):
                    add(
                        f"Фид: {title}",
                        "warn",
                        f"Последняя загрузка не удалась — работаем с файлом {_ago(meta.get('fetched_at'), now)}",
                    )
                elif age > max_age * 2:
                    add(f"Фид: {title}", "warn", f"Фид не обновлялся {_ago(meta.get('fetched_at'), now)}")
            except (OSError, ValueError):
                pass

    # 4. источники на паузе
    for b in guard.states(con):
        if b["state"] == "open" and b["until"] and b["until"] > now:
            until = time.strftime("%d.%m %H:%M", time.localtime(b["until"]))
            add(
                f"Источник {sites.SITES.get(b['site'], {}).get('title', b['site'])} / "
                f"{'фид' if b['source'] == 'feed' else 'сайт'}",
                "warn",
                f"На паузе до {until}: {b['reason']}",
            )

    # 5. база, диск, копии
    q = backup.quick_check(con)
    add("База данных", "ok" if q == "ok" else "fail", "Целостность в порядке" if q == "ok" else f"Повреждения: {q}")
    try:
        free = shutil.disk_usage(config.DATA).free
        status = "ok" if free > 300e6 else ("warn" if free > 50e6 else "fail")
        add("Место на диске", status, f"Свободно {free / 1e9:.1f} ГБ")
    except OSError:
        pass
    b_ts = backup.last_ts()
    if ok_run:
        if not b_ts:
            add("Резервная копия", "warn", "Копий ещё нет")
        elif now - b_ts > 8 * 86400:
            add("Резервная копия", "warn", f"Последняя копия {_ago(b_ts, now)}")
        else:
            add("Резервная копия", "ok", f"Последняя копия {_ago(b_ts, now)}")
        mdir = backup.mirror_folder(settings)
        if mdir and b_ts:
            m = backup.items(mdir) if os.path.isdir(mdir) else []
            if not m:
                add("Копия во второй папке", "warn", f"Во второй папке копий нет: {mdir} — папка недоступна?")
            elif b_ts - m[0]["ts"] > 2 * 86400:
                add("Копия во второй папке", "warn", f"Последняя копия во второй папке {_ago(m[0]['ts'], now)}")
            else:
                add("Копия во второй папке", "ok", f"Последняя копия {_ago(m[0]['ts'], now)}")

    worst = max((c["status"] for c in checks), key=lambda s: ORDER[s], default="ok")
    return {"status": worst, "checks": checks, "ts": now, "version": config.VERSION}


# ---------------------------------------------------------------- уведомления


def _state_path() -> str:
    return os.path.join(config.DATA, STATE_FILE)


def _load_state() -> dict[str, Any]:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def notify_windows(title: str, text: str) -> bool:
    """Всплывающее уведомление Windows 10/11 через PowerShell, без сторонних модулей."""
    if not sys.platform.startswith("win"):
        return False
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")  # noqa: E731
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        f'$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml(\'<toast><visual><binding template="ToastGeneric">'
        f"<text>{esc(title)}</text><text>{esc(text)}</text></binding></visual></toast>');"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            creationflags=0x08000000,
        )  # CREATE_NO_WINDOW
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify_webhook(url: str, text: str) -> bool:
    """POST {"text": …} или GET с подстановкой {text} (так работает, например, Telegram-бот)."""
    if not url:
        return False
    try:
        if "{text}" in url:
            req = urllib.request.Request(url.replace("{text}", urllib.parse.quote(text)))
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except (OSError, ValueError):
        return False


def alert(settings: dict, title: str, text: str) -> list[str]:
    sent = []
    a = settings.get("alerts", {})
    if a.get("windows", True) and notify_windows(title, text):
        sent.append("windows")
    if a.get("webhook_url") and notify_webhook(a["webhook_url"], f"{title}\n{text}"):
        sent.append("webhook")
    level = "INFO" if "в порядке" in title else "ERROR"
    log.write(level, "alert.sent", f"{title}: {text}", channels=sent)
    return sent


def watchdog(con: sqlite3.Connection, settings: dict | None = None, now: float | None = None) -> dict[str, Any]:
    """Dead man's switch: тревога, если сбор не проходил или здоровье «плохо». Повтор — раз в сутки."""
    settings = settings or config.load()
    now = now or time.time()
    h = check(con, settings, now)
    bad = [c for c in h["checks"] if c["status"] == "fail"]
    state = _load_state()
    signature = "|".join(sorted(f"{c['name']}" for c in bad))
    result: dict[str, Any] = {"status": h["status"], "alerted": False, "channels": []}
    if bad:
        if signature != state.get("signature") or now - state.get("ts", 0) >= REPEAT_S:
            text = "; ".join(f"{c['name']}: {c['detail']}" for c in bad)
            result["channels"] = alert(settings, "Монитор Основит: сбор цен не в порядке", text)
            result["alerted"] = True
            state = {"signature": signature, "ts": now, "open": True}
            _save_state(state)
    elif state.get("open"):
        result["channels"] = alert(settings, "Монитор Основит: снова всё в порядке", "Сбор цен прошёл, тревога снята")
        result["alerted"] = True
        _save_state({"signature": "", "ts": now, "open": False})
    log.info("watchdog.run", f"Проверка здоровья: {h['status']}", status=h["status"], alerted=result["alerted"])
    result["health"] = h
    return result
