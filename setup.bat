@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Virtualenv...
py -3.11 -m venv .venv
if errorlevel 1 py -m venv .venv
if errorlevel 1 (
  echo Python bulunamadi. https://www.python.org/downloads/
  pause
  exit /b 1
)

echo [2/3] Paketler...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip hata
  pause
  exit /b 1
)

echo [3/3] Chromium...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo playwright hata
  pause
  exit /b 1
)

echo.
echo OK. Sonra:
echo   1^) secrets.env icine GEMINI_API_KEY ve START_URL yaz
echo   2^) run.bat cift tikla
echo.
pause
