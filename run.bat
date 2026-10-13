@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo  BOT v5.2-parallel  (iki kurs, iki sekme, ayni anda)
echo  Once 1_chrome_ac.bat, giris yap, kursu ac
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo Once setup.bat
  pause
  exit /b 1
)

powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9222/json/version -TimeoutSec 2).StatusCode } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo Chrome debug kapali. Once 1_chrome_ac.bat
  pause
  exit /b 1
)

if exist "__pycache__" rmdir /s /q "__pycache__" 2>nul

if "%~1"=="" (
  ".venv\Scripts\python.exe" -B -u bot.py
) else (
  ".venv\Scripts\python.exe" -B -u bot.py --scale %~1
)

echo.
pause
