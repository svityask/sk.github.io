@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Монитор Основит — Петрович и Лемана ПРО

rem 1) переносной Python в папке python (как в версии 2.0.0)
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" "%~dp0start.py" %*
  goto :end
)
rem 2) установленный Python
where py >nul 2>nul && (
  py -3 "%~dp0start.py" %*
  goto :end
)
where python >nul 2>nul && (
  python "%~dp0start.py" %*
  goto :end
)
echo.
echo Не найден Python.
echo Скопируйте папку "python" из версии 2.0.0 сюда, рядом с этим файлом,
echo или установите Python 3.12 с python.org.
echo.
pause
:end
if errorlevel 1 if not "%~1"=="--run" pause
