"""Запуск из Запустить.cmd. Сам добавляет свою папку в путь — переносной Python этого не делает."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

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
