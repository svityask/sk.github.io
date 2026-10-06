"""Резервные копии базы и настроек.

Копия — ZIP в data/backups: monitor.sqlite (снимок через SQLite backup API, согласованный даже во время работы)
и файл настроек. Перед копией — PRAGMA quick_check: испорченную базу не копируем, чтобы не вытеснить хорошие копии.
Хранение: KEEP_LAST последних плюс по одной (самой новой) за каждую из KEEP_WEEKS последних недель.
Вторая папка (настройка backup.mirror_dir — сетевой диск, OneDrive, Яндекс Диск): каждая копия кладётся и туда,
в подпапку MIRROR_SUBDIR, с тем же правилом хранения. Недоступна вторая папка — копия всё равно есть локально.
Восстановление — только из командной строки при закрытом приложении: start.py --restore <файл.zip>.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime

from . import config, log

KEEP_LAST = 10
KEEP_WEEKS = 8
PREFIX = "monitor-"
MIRROR_SUBDIR = "Монитор Основит DIY — копии"


def folder() -> str:
    return os.path.join(config.DATA, "backups")


def quick_check(con: sqlite3.Connection) -> str:
    rows = [r[0] for r in con.execute("PRAGMA quick_check").fetchall()]
    return "ok" if rows == ["ok"] else "; ".join(rows[:5])


def mirror_folder(settings: dict | None = None) -> str | None:
    """Папка копий во второй папке или None, если вторая папка не задана."""
    settings = settings or config.load()
    root = (settings.get("backup") or {}).get("mirror_dir") or ""
    return os.path.join(root, MIRROR_SUBDIR) if root.strip() else None


def items(base: str | None = None) -> list[dict]:
    base = base or folder()
    if not os.path.isdir(base):
        return []
    out = []
    for name in os.listdir(base):
        if name.startswith(PREFIX) and name.endswith(".zip"):
            p = os.path.join(base, name)
            out.append({"name": name, "path": p, "ts": os.path.getmtime(p), "size": os.path.getsize(p)})
    return sorted(out, key=lambda b: b["ts"], reverse=True)


def last_ts() -> float | None:
    it = items()
    return it[0]["ts"] if it else None


def due(min_interval_h: float = 20) -> bool:
    ts = last_ts()
    return ts is None or time.time() - ts >= min_interval_h * 3600


def make(con: sqlite3.Connection, reason: str = "после сбора", settings: dict | None = None) -> str | None:
    check = quick_check(con)
    if check != "ok":
        log.error("backup.skipped", f"База не прошла проверку целостности — копию не делаем: {check}")
        return None
    os.makedirs(folder(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(folder(), f"{PREFIX}{stamp}.zip")
    with tempfile.TemporaryDirectory(dir=folder()) as tmp:
        snap = os.path.join(tmp, "monitor.sqlite")
        dst = sqlite3.connect(snap)
        try:
            con.backup(dst)
        finally:
            dst.close()
        try:
            with zipfile.ZipFile(target + ".part", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "monitor.sqlite")
                if os.path.exists(config.SETTINGS_PATH):
                    z.write(config.SETTINGS_PATH, os.path.basename(config.SETTINGS_PATH))
                z.writestr(
                    "README.txt",
                    f"Резервная копия Монитора Основит DIY {config.VERSION}, {stamp} ({reason}).\n"
                    'Восстановить: Запустить.cmd --restore "путь к этому файлу" (приложение закрыто).\n',
                )
        except BaseException:
            if os.path.exists(target + ".part"):  # кончилось место и т. п. — недописанную копию не оставляем
                os.remove(target + ".part")
            raise
    os.replace(target + ".part", target)
    removed = prune()
    mirror(target, settings)
    log.info(
        "backup.done",
        "Резервная копия сделана",
        path=target,
        bytes=os.path.getsize(target),
        removed=removed,
        reason=reason,
    )
    return target


def mirror(path: str, settings: dict | None = None) -> str | None:
    """Кладёт копию во вторую папку. Ошибка — только запись в журнал: локальная копия уже есть."""
    dest_dir = mirror_folder(settings)
    if not dest_dir:
        return None
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(path))
        shutil.copyfile(path, dest + ".part")
        os.replace(dest + ".part", dest)
        prune(base=dest_dir)
        log.info("backup.mirror", "Копия положена во вторую папку", path=dest)
        return dest
    except OSError as e:
        log.warning("backup.mirror_failed", f"Вторая папка для копий недоступна: {e}", folder=dest_dir)
        return None


def prune(keep_last: int = KEEP_LAST, keep_weeks: int = KEEP_WEEKS, base: str | None = None) -> int:
    all_items = items(base)
    keep = {b["path"] for b in all_items[:keep_last]}
    weeks: dict[tuple[int, int], str] = {}
    for b in all_items:  # от новых к старым: первая встреченная в неделе — самая новая
        wk = datetime.fromtimestamp(b["ts"]).isocalendar()[:2]
        if wk not in weeks and len(weeks) < keep_weeks:
            weeks[wk] = b["path"]
    keep |= set(weeks.values())
    removed = 0
    for b in all_items:
        if b["path"] not in keep:
            os.remove(b["path"])
            removed += 1
    return removed


def restore(zip_path: str, db_path: str | None = None) -> str:
    """Восстанавливает базу и настройки из копии. Текущая база сохраняется рядом (before-restore-…)."""
    db_path = db_path or config.DB_PATH
    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as tmp:
        z.extract("monitor.sqlite", tmp)
        snap = os.path.join(tmp, "monitor.sqlite")
        test = sqlite3.connect(snap)
        try:
            check = quick_check(test)
        finally:
            test.close()
        if check != "ok":
            raise ValueError(f"Копия повреждена: {check}")
        os.makedirs(folder(), exist_ok=True)
        if os.path.exists(db_path):
            saved = os.path.join(folder(), f"before-restore-{datetime.now():%Y%m%d-%H%M%S}.sqlite")
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(saved)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        for ext in ("-wal", "-shm"):
            if os.path.exists(db_path + ext):
                os.remove(db_path + ext)
        shutil.copyfile(snap, db_path)
        settings_name = os.path.basename(config.SETTINGS_PATH)
        if settings_name in z.namelist():
            z.extract(settings_name, tmp)
            shutil.copyfile(os.path.join(tmp, settings_name), config.SETTINGS_PATH)
    log.warning("backup.restored", "База восстановлена из резервной копии", path=zip_path)
    return db_path
