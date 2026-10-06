"""Сборка архива для пользователя: dist/Монитор-Основит-DIY-<версия>.zip.

Внутри — папка «Монитор Основит DIY <версия>» с приложением, документацией и тестами. Папки data и python,
кэши и служебные файлы разработки в архив не попадают.

    python tools/build.py
"""

from __future__ import annotations

import os
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


def build(dest_dir: str | None = None) -> str:
    dest_dir = dest_dir or os.path.join(ROOT, "dist")
    os.makedirs(dest_dir, exist_ok=True)
    folder = f"Монитор Основит DIY {config.VERSION}"
    target = os.path.join(dest_dir, f"Монитор-Основит-DIY-{config.VERSION}.zip")
    with zipfile.ZipFile(target + ".part", "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sources():
            z.write(os.path.join(ROOT, rel), f"{folder}/{rel.replace(os.sep, '/')}")
    os.replace(target + ".part", target)
    return target


if __name__ == "__main__":
    path = build()
    print(f"{path} ({os.path.getsize(path) / 1024:.0f} КБ)")
