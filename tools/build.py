"""Сборка для пользователя.

    python tools/build.py                          dist/Osnovit-DIY-<версия>.zip — без Python
    python tools/build.py --python DIR             dist/Osnovit-DIY-<версия>-Windows.zip — с переносным
                                                   Python внутри (папка python): распаковал и запустил
    python tools/build.py --python DIR --stage D   то же содержимое развёрнутой папкой D — для установщика

Внутри архива — папка «Монитор Основит DIY <версия>» с приложением, документацией и тестами. Папка data,
кэши и служебные файлы разработки в сборку не попадают никогда: данные пользователя живут только у него.
Имена файлов сборки латиницей: GitHub в выпусках заменяет кириллицу в именах, а почта и мессенджеры её портят.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from monitor import config  # noqa: E402

FILES = ("Запустить.cmd", "start.py", "README.md", "CHANGELOG.md")
DIRS = ("monitor", "ui", "docs", "tests")
SKIP_DIRS = {"__pycache__", ".mypy_cache", ".ruff_cache"}


def sources() -> list[str]:
    out = [f for f in FILES if os.path.exists(os.path.join(ROOT, f))]
    for d in DIRS:
        for base, dirs, files in os.walk(os.path.join(ROOT, d)):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            out += [os.path.relpath(os.path.join(base, f), ROOT) for f in sorted(files) if not f.endswith(".pyc")]
    return out


def python_files(python_dir: str) -> list[tuple[str, str]]:
    """(путь на диске, путь в сборке) для переносного Python: всё содержимое папки → python/…"""
    if not os.path.isfile(os.path.join(python_dir, "python.exe")):
        raise SystemExit(f"В {python_dir} нет python.exe — это не переносной Python для Windows")
    out = []
    for base, dirs, files in os.walk(python_dir):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for f in sorted(files):
            full = os.path.join(base, f)
            out.append((full, os.path.join("python", os.path.relpath(full, python_dir))))
    return out


def entries(python_dir: str | None = None) -> list[tuple[str, str]]:
    items = [(os.path.join(ROOT, rel), rel) for rel in sources()]
    if python_dir:
        items += python_files(python_dir)
    return items


def build(dest_dir: str | None = None, python_dir: str | None = None) -> str:
    dest_dir = dest_dir or os.path.join(ROOT, "dist")
    os.makedirs(dest_dir, exist_ok=True)
    folder = f"Монитор Основит DIY {config.VERSION}"
    suffix = "-Windows" if python_dir else ""
    target = os.path.join(dest_dir, f"Osnovit-DIY-{config.VERSION}{suffix}.zip")
    with zipfile.ZipFile(target + ".part", "w", zipfile.ZIP_DEFLATED) as z:
        for src, rel in entries(python_dir):
            z.write(src, f"{folder}/{rel.replace(os.sep, '/')}")
    os.replace(target + ".part", target)
    return target


def stage(dest: str, python_dir: str | None = None) -> str:
    """Развёрнутая папка сборки (для установщика). Папка пересоздаётся с нуля."""
    shutil.rmtree(dest, ignore_errors=True)
    for src, rel in entries(python_dir):
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(src, out)
    return dest


def utf8_output() -> None:
    """Логи CI на Windows идут через канал в кодировке ANSI — русский текст выводим в UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Сборка Монитора Основит DIY")
    ap.add_argument("--python", metavar="DIR", help="папка переносного Python для Windows (python.exe внутри)")
    ap.add_argument("--stage", metavar="DIR", help="ещё и развёрнутая папка для установщика")
    ap.add_argument("--dist", metavar="DIR", help="куда положить архив (по умолчанию dist/)")
    a = ap.parse_args(argv)
    utf8_output()
    path = build(a.dist, a.python)
    print(f"{path} ({os.path.getsize(path) / 1024 / 1024:.1f} МБ)")
    if a.stage:
        print(f"Развёрнуто: {stage(a.stage, a.python)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
