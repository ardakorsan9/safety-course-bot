@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo  2^) Bot — senin acik Chrome'una baglan
echo ========================================
echo.
echo Once 1_chrome_ac.bat ile Chrome acilmis olmali.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [HATA] Once setup.bat calistir.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -u bot.py --attach
set ERR=%ERRORLEVEL%
echo.
if not "%ERR%"=="0" echo [HATA] Cikis kodu: %ERR%
pause
exit /b %ERR%
