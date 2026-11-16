@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Собираю AI_Marketer.exe — одно приложение для Windows.
py -3 -m pip install --upgrade pip
py -3 -m pip install pyinstaller
py -3 -m PyInstaller --noconfirm --onefile --name AI_Marketer ai_marketer_one.py
echo.
if exist dist\AI_Marketer.exe (
  echo Готово: dist\AI_Marketer.exe
  echo Скопируйте этот файл куда удобно и запускайте двойным щелчком.
) else (
  echo Сборка не удалась — смотрите сообщения выше.
)
pause
