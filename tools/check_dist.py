"""Проверка готовой сборки так, как её получит пользователь (запускается в CI на Windows).

    python tools/check_dist.py zip       ФАЙЛ.zip     распаковать и проверить
    python tools/check_dist.py installer ФАЙЛ.exe     поставить, обновить поверх, удалить — и проверить всё

Проверяется именно сборка, а не исходники: запуск через Запустить.cmd и встроенный python\\python.exe,
проверка компьютера, сбор цен из тестового фида с отчётом Excel, резервная копия; для установщика — ещё
повторная установка поверх запущенной программы (данные целы), расписание и удаление при запущенной программе
(процессы закрыты, задачи сняты, данные остались). Сборка с Python ещё прогоняет офлайн-тесты на нём самом.
На Linux/macOS — тот же сценарий без Запустить.cmd и встроенного Python (для проверки самой логики).
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
import time
import zipfile

WIN = sys.platform.startswith("win")
TIMEOUT = 600


def fail(msg: str) -> None:
    raise SystemExit(f"ПРОВЕРКА НЕ ПРОЙДЕНА: {msg}")


def run_app(app: str, *args: str, ok=(0,)) -> subprocess.CompletedProcess:
    """Запуск так, как запускает человек или Планировщик: Запустить.cmd (на Windows) с аргументами."""
    if WIN:
        cmd = ["cmd", "/c", os.path.join(app, "Запустить.cmd"), *args]
    else:
        cmd = [sys.executable, os.path.join(app, "start.py"), *args]
    # stdin пустой: «pause» в Запустить.cmd при ошибке не должен подвесить проверку
    r = subprocess.run(cmd, cwd=app, capture_output=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    print(f"$ Запустить.cmd {' '.join(args)} → код {r.returncode}\n{out[-1500:]}")
    if r.returncode not in ok:
        fail(f"{' '.join(args)} завершился с кодом {r.returncode}")
    return r


def app_python(app: str) -> list[str]:
    exe = os.path.join(app, "python", "python.exe")
    if WIN:
        if not os.path.isfile(exe):
            fail("в сборке нет python\\python.exe")
        return [exe]
    return [sys.executable]


def configure(app: str) -> str:
    """Настройки как у пользователя: фид Лемана ПРО из тестового файла, категория «Штукатурки»."""
    report_dir = os.path.join(app, "data", "отчёты-проверка")
    code = f"""
import sys; sys.path.insert(0, {app!r}); sys.stdout.reconfigure(encoding="utf-8")
from monitor import config, db
s = config.load()
s["report_dir"] = {report_dir!r}
s["sites"]["petrovich"]["enabled"] = False
s["sites"]["lemanapro"].update(feed={os.path.join(app, "tests", "fixtures", "lemanapro_admitad.yml")!r},
                               feed_city="Москва", edge_enabled=False)
config.save(s)
con = db.connect(config.DB_PATH)
db.add_tracked(con, "lemanapro", "category", "111", "Штукатурки")
con.close()
print("настроено:", config.DB_PATH)
"""
    r = subprocess.run([*app_python(app), "-c", code], cwd=app, capture_output=True, timeout=120)
    print((r.stdout + r.stderr).decode("utf-8", "replace"))
    if r.returncode:
        fail("не удалось подготовить настройки")
    return report_dir


def check_app(app: str) -> None:
    """Общая проверка распакованной или установленной программы."""
    if not os.path.isfile(os.path.join(app, "Запустить.cmd")):
        fail(f"в {app} нет Запустить.cmd")
    # код 1–2 бывает из-за окружения (фиды не заданы, сайты сетей не отвечают машине сборки) — проверяем, что
    # проверка дошла до конца, а Python, папки и база у программы в порядке
    out = run_app(app, "--selftest", ok=(0, 1, 2)).stdout.decode("utf-8", "replace")
    for item in ("Python", "Папка данных", "Место на диске", "База данных"):
        if f"[в порядке] {item}" not in out:
            fail(f"проверка компьютера: «{item}» не в порядке")
    report_dir = configure(app)
    run_app(app, "--run")
    reports = glob.glob(os.path.join(report_dir, "*.xlsx"))
    if not reports:
        fail("сбор прошёл, а отчёта Excel нет")
    with zipfile.ZipFile(reports[0]) as z:
        if "xl/workbook.xml" not in z.namelist():
            fail("отчёт Excel повреждён")
    run_app(app, "--health", ok=(0, 1))
    run_app(app, "--backup")
    print(f"Программа в {app} работает: отчёт {os.path.basename(reports[0])}")


def run_tests(app: str) -> None:
    """Офлайн-тесты на том самом Python, что лежит в сборке (тест окна браузера — отдельно, в CI)."""
    if not WIN:
        return
    r = subprocess.run(
        [*app_python(app), "-m", "unittest", "discover", "-s", "tests", "-p", "test_[!b]*.py"],
        cwd=app,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=TIMEOUT,
    )
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    print(out[-1500:])
    if r.returncode:
        fail("офлайн-тесты на Python из сборки не прошли")


def check_zip(path: str) -> None:
    tmp = tempfile.mkdtemp(prefix="osnovit-zip-")
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp)
    roots = [os.path.join(tmp, d) for d in os.listdir(tmp)]
    if len(roots) != 1:
        fail(f"в архиве должна быть одна папка программы, а их {len(roots)}")
    app = roots[0]
    if os.path.exists(os.path.join(app, "data")):
        fail("в архиве есть папка data — данные разработчика попали в сборку")
    run_tests(app)
    check_app(app)


# ---------------------------------------------------------------- установщик (только Windows)


def wait_gone(path: str, seconds: int = 180) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not os.path.exists(path):
            return
        time.sleep(2)
    fail(f"{path} не удалён за {seconds} с")


def task_exists(app: str) -> bool:
    code = f"import sys; sys.path.insert(0, {app!r}); from monitor import schedule; print(schedule.state())"
    r = subprocess.run([*app_python(app), "-c", code], cwd=app, capture_output=True, text=True, timeout=60)
    print("расписание:", r.stdout.strip(), r.stderr.strip())
    return "'enabled': True" in r.stdout


def busy(app: str) -> subprocess.Popen:
    """Процесс Python программы, как будто окно открыто или идёт сбор: держит занятыми файлы в папке python."""
    p = subprocess.Popen([*app_python(app), "-c", "import time; time.sleep(900)"], cwd=app)
    time.sleep(2)
    if p.poll() is not None:
        fail("не удалось запустить Python программы")
    return p


def check_installer(setup: str) -> None:
    if not WIN:
        fail("установщик проверяется только на Windows")
    base = tempfile.mkdtemp(prefix="osnovit-inst-")
    app = os.path.join(base, "Монитор Основит DIY — проверка")  # кириллица и пробелы в пути — как у людей
    args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={app}", f"/LOG={os.path.join(base, 'setup.log')}"]

    print("== установка")
    r = subprocess.run([setup, *args], timeout=TIMEOUT)
    if r.returncode:
        fail(f"установщик завершился с кодом {r.returncode}")
    menu = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs")
    if not glob.glob(os.path.join(menu, "Монитор Основит DIY*.lnk")):
        fail("нет ярлыка в меню «Пуск»")
    check_app(app)
    db_files = glob.glob(os.path.join(app, "data", "*.sqlite"))
    if not db_files:
        fail("после сбора нет базы в data")

    print("== обновление поверх запущенной программы: она закрывается, данные остаются")
    stamp = os.path.getsize(db_files[0])
    p = busy(app)
    r = subprocess.run([setup, *args], timeout=TIMEOUT)
    if p.poll() is None:
        p.kill()
        fail("установщик не закрыл запущенную программу")
    if r.returncode or not os.path.exists(db_files[0]) or os.path.getsize(db_files[0]) < stamp:
        fail("повторная установка повредила или удалила базу")
    run_app(app, "--run")

    print("== расписание и удаление")
    code = (
        f"import sys; sys.path.insert(0, {app!r}); from monitor import schedule; "
        "print(schedule.set_schedule(True, '08:30', True, False))"
    )
    subprocess.run([*app_python(app), "-c", code], cwd=app, timeout=60, check=True)
    if not task_exists(app):
        fail("расписание не включилось — проверить его снятие нельзя")
    unins = glob.glob(os.path.join(app, "unins*.exe"))
    if not unins:
        fail("нет программы удаления")
    p = busy(app)
    subprocess.run([unins[0], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=TIMEOUT)
    wait_gone(unins[0])
    wait_gone(os.path.join(app, "monitor"))
    wait_gone(os.path.join(app, "python"))
    if p.poll() is None:
        p.kill()
        fail("удаление не закрыло запущенную программу")
    if not os.path.exists(db_files[0]):
        fail("удаление стёрло собранные данные (при тихом удалении они должны остаться)")
    for name in ("Монитор Основит — Петрович и Лемана ПРО", "Монитор Основит — Петрович и Лемана ПРО — проверка"):
        q = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True)
        if q.returncode == 0:
            fail(f"после удаления осталась задача Планировщика «{name}»")
    print("Установщик: установка, обновление поверх, расписание и удаление — в порядке; данные сохранены")


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
    if len(args) != 2 or args[0] not in ("zip", "installer"):
        print(__doc__)
        return 2
    (check_zip if args[0] == "zip" else check_installer)(os.path.abspath(args[1]))
    print("ПРОВЕРКА ПРОЙДЕНА")
    return 0


if __name__ == "__main__":
    sys.exit(main())
