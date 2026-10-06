"""Расписание в Планировщике Windows: ежедневный сбор и проверка «сбор прошёл?» (dead man's switch).

Задачи создаются из XML, а не одной строкой schtasks: так можно задать то, чего нет в параметрах командной строки:
- пропущенный запуск (компьютер был выключен или спал) выполняется сразу после включения (StartWhenAvailable);
- ноутбук собирает и от батареи (по умолчанию Планировщик не запускает задачи от батареи);
- по желанию — разбудить компьютер из сна (WakeToRun);
- второй экземпляр не запускается, если первый ещё идёт; сбор дольше 2 часов прерывается.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape

from . import config, log

TASK_NAME = "Монитор Основит — Петрович и Лемана ПРО"
WATCHDOG_NAME = TASK_NAME + " — проверка"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def launcher() -> str:
    return os.path.join(config.ROOT, "Запустить.cmd")


def parse_at(at: str | None) -> tuple[int, int]:
    """«8:30», «08.30», «0830» → (8, 30). ValueError с понятным текстом, если это не время суток."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:.]?\s*(\d{2})\s*", at or "08:30")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"не понял время «{at}» — напишите, например, 08:30")
    return int(m.group(1)), int(m.group(2))


def _start_boundary(at: str) -> str:
    hh, mm = parse_at(at)
    return time.strftime("%Y-%m-%d") + f"T{hh:02d}:{mm:02d}:00"


def _decode(raw: bytes) -> str:
    """Вывод schtasks: UTF-8, если окно переключено chcp 65001 (так делает Запустить.cmd), иначе OEM-кодировка."""
    for enc in ("utf-8", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp866", "replace")


def build_xml(kind: str, at: str = "08:30", wake: bool = False, command: str | None = None) -> str:
    """XML задачи. kind: 'run' — сбор каждый день в at; 'watchdog' — проверка каждые 6 часов."""
    command = command or launcher()
    if kind == "run":
        trigger = (
            "<CalendarTrigger>"
            f"<StartBoundary>{_start_boundary(at)}</StartBoundary><Enabled>true</Enabled>"
            "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
            "</CalendarTrigger>"
        )
        args, limit, desc = "--run", "PT2H", "Сбор цен Петрович и Лемана ПРО каждый день"
    elif kind == "watchdog":
        trigger = (
            "<TimeTrigger>"
            "<Repetition><Interval>PT6H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
            f"<StartBoundary>{_start_boundary('00:15')}</StartBoundary><Enabled>true</Enabled>"
            "</TimeTrigger>"
        )
        args, limit, desc = "--watchdog", "PT10M", "Проверка: прошёл ли сбор цен (уведомление, если нет)"
        wake = False  # будить компьютер ради проверки не нужно
    else:
        raise ValueError(kind)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{NS}">'
        f"<RegistrationInfo><Description>{escape(desc)}</Description></RegistrationInfo>"
        f"<Triggers>{trigger}</Triggers>"
        '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>"
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
        f"<WakeToRun>{'true' if wake else 'false'}</WakeToRun>"
        f"<ExecutionTimeLimit>{limit}</ExecutionTimeLimit>"
        "<Enabled>true</Enabled>"
        "</Settings>"
        '<Actions Context="Author"><Exec>'
        f"<Command>{escape(command)}</Command><Arguments>{args}</Arguments>"
        f"<WorkingDirectory>{escape(os.path.dirname(command))}</WorkingDirectory>"
        "</Exec></Actions>"
        "</Task>"
    )


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["schtasks", *args], capture_output=True, timeout=30)
    return subprocess.CompletedProcess(r.args, r.returncode, _decode(r.stdout or b""), _decode(r.stderr or b""))


def _create(name: str, xml: str) -> subprocess.CompletedProcess:
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:  # Планировщик ждёт UTF-16 с BOM
            f.write(xml)
        return _schtasks("/Create", "/TN", name, "/XML", path, "/F")
    finally:
        os.remove(path)


def state() -> dict:
    if not sys.platform.startswith("win"):
        return {"available": False}
    try:
        return {
            "available": True,
            "enabled": _schtasks("/Query", "/TN", TASK_NAME).returncode == 0,
            "watchdog": _schtasks("/Query", "/TN", WATCHDOG_NAME).returncode == 0,
        }
    except (OSError, subprocess.SubprocessError):
        return {"available": False}


def set_schedule(enabled: bool, at: str = "08:30", watchdog: bool = True, wake: bool = False) -> dict:
    if not sys.platform.startswith("win"):
        return {"ok": False, "error": "Расписание доступно только в Windows"}
    if enabled:
        try:
            hh, mm = parse_at(at)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        at = f"{hh:02d}:{mm:02d}"
    else:
        _schtasks("/Delete", "/TN", TASK_NAME, "/F")
        _schtasks("/Delete", "/TN", WATCHDOG_NAME, "/F")
        log.info("schedule.off", "Расписание выключено")
        return {"ok": True}
    try:
        r = _create(TASK_NAME, build_xml("run", at, wake))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"ok": False, "error": f"Планировщик недоступен: {e}"}
    if r.returncode:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()}
    if watchdog:
        w = _create(WATCHDOG_NAME, build_xml("watchdog"))
        if w.returncode:
            return {
                "ok": False,
                "error": "Сбор по расписанию включён, а проверка — нет: " + (w.stderr or w.stdout).strip(),
            }
    else:
        _schtasks("/Delete", "/TN", WATCHDOG_NAME, "/F")
    log.info(
        "schedule.on",
        f"Расписание: сбор каждый день в {at}"
        + (", будить компьютер" if wake else "")
        + (", проверка каждые 6 ч" if watchdog else ""),
    )
    return {"ok": True}


def upgrade() -> bool:
    """Задачи из версий до 2.4 (созданы одной строкой schtasks) пересоздаёт из XML с тем же временем.

    Признак старой задачи — нет StartWhenAvailable. Возвращает True, если пересоздали.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        q = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
        if q.returncode or "<StartWhenAvailable>true</StartWhenAvailable>" in q.stdout:
            return False
        m = re.search(r"<StartBoundary>\d{4}-\d\d-\d\dT(\d\d):(\d\d)", q.stdout)
        at = f"{m.group(1)}:{m.group(2)}" if m else (config.load().get("schedule") or {}).get("at") or "08:30"
        watchdog = _schtasks("/Query", "/TN", WATCHDOG_NAME).returncode == 0
        r = set_schedule(True, at, watchdog, bool((config.load().get("schedule") or {}).get("wake")))
    except (OSError, subprocess.SubprocessError):
        return False
    if r.get("ok"):
        log.info("schedule.upgraded", f"Задачи Планировщика пересозданы по правилам 2.4, сбор в {at}")
    return bool(r.get("ok"))
