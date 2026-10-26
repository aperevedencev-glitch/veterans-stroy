@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (echo Сначала запустите "1 Проверка токена.bat" & pause & exit /b 1)
echo Пересобираю найденные договоры из уже скачанного, с отбором по Градостроительному кодексу. Токен не нужен.
"%~dp0.venv\Scripts\python.exe" run.py rebuild
echo.
echo Готово. Excel для оператора - в папке data\out
pause
