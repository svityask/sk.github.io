"""Переносной Python для Windows (official embeddable package) — для сборки «распаковал и запустил».

    python tools/fetch_python.py DIR            скачать и распаковать в DIR, проверить подпись и модули

Берём ровно указанную версию с python.org, а не «что придёт»: сборка должна быть повторяемой.
Проверки, без которых в сборку Python не попадает:
  • python.exe подписан Python Software Foundation и подпись действительна (Windows, Authenticode);
  • в нём есть всё, что нужно приложению: sqlite3, ssl, xml, gzip/zipfile, json — и он запускает start.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile

# 3.12.10 — последний выпуск ветки 3.12 с готовыми сборками для Windows (дальше у 3.12 только исправления
# безопасности в исходниках). Менять осознанно: CI проверит новую версию теми же тестами.
VERSION = os.environ.get("PYTHON_EMBED_VERSION", "3.12.10")
URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
SIGNER = "Python Software Foundation"
MODULES = "import sqlite3, ssl, xml.etree.ElementTree, gzip, zipfile, json, urllib.request, http.server, csv"


def download(dest: str) -> str:
    url = URL.format(v=VERSION)
    print(f"Скачиваю {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    if len(data) < 5_000_000:
        raise SystemExit(f"Подозрительно маленький файл ({len(data)} байт) — это не дистрибутив Python")
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)
    return dest


def check_signature(python_exe: str) -> None:
    """Подпись Authenticode: только на Windows (на других системах проверить нечем — сборка всё равно для Windows)."""
    if not sys.platform.startswith("win"):
        print("Подпись не проверена: проверка Authenticode есть только в Windows")
        return
    ps = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{python_exe}'; "
        "@{status = [string]$s.Status; subject = [string]$s.SignerCertificate.Subject} | ConvertTo-Json"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    if info.get("status") != "Valid" or SIGNER not in (info.get("subject") or ""):
        raise SystemExit(f"python.exe не подписан {SIGNER}: {info}")
    print(f"Подпись python.exe действительна: {info['subject']}")


def check_modules(python_exe: str) -> None:
    out = subprocess.run(
        [python_exe, "-c", MODULES + "; import sys; print(sys.version.split()[0], sqlite3.sqlite_version)"],
        capture_output=True,
        text=True,
    )
    if out.returncode:
        raise SystemExit(f"В переносном Python не хватает модулей: {out.stderr.strip()}")
    print(f"Python {out.stdout.strip()} — модули на месте")


def utf8_output() -> None:
    """Логи CI на Windows идут через канал в кодировке ANSI — русский текст выводим в UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    dest = os.path.abspath(args[0])
    download(dest)
    exe = os.path.join(dest, "python.exe")
    check_signature(exe)
    if sys.platform.startswith("win"):
        check_modules(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
