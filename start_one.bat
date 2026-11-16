@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запускаю AI-маркетолога (один файл)...
py -3 ai_marketer_one.py %*
if errorlevel 1 (
  echo.
  echo Не удалось запустить. Проверьте, что установлен Python 3.9+
  echo Скачать: https://www.python.org/downloads/windows/
)
pause
