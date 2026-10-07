"""Запуск из Запустить.cmd. Сам добавляет свою папку в путь — переносной Python этого не делает."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# вывод в файл или в другую программу (Планировщик, проверка сборки) — в UTF-8, а не в кодировке Windows:
# иначе первая же русская строка там роняет программу
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

if sys.version_info < (3, 10):  # noqa: UP036 — переносной Python может оказаться старым
    print("Нужен Python 3.10 или новее. Сейчас:", sys.version.split()[0])
    sys.exit(2)
try:
    import sqlite3  # noqa: F401
except ImportError:
    print(
        "В этом Python нет модуля sqlite3 (его вырезали из переносной сборки).\n"
        "Положите в папку python полный «Windows embeddable package» с python.org "
        "или установите Python 3.12 — тогда Запустить.cmd найдёт его сам."
    )
    sys.exit(2)

from monitor.app import main  # noqa: E402

sys.exit(main())
