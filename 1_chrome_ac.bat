@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo  1^) Chrome'u DEBUG modda ac
echo ========================================
echo.
echo ONEMLI: Once tum Chrome pencerelerini KAPAT.
echo Sonra bu script Chrome'u 9222 portuyla acar.
echo Sen siteye giris yap, Next'i gor, sonra run.bat calistir.
echo.

set "PORT=9222"
set "PROFILE=%~dp0.chrome-attach-profile"

REM Chrome yollari
set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if "%CHROME%"=="" (
  echo [HATA] chrome.exe bulunamadi.
  pause
  exit /b 1
)

echo Chrome: %CHROME%
echo Profil: %PROFILE%
echo Port:   %PORT%
echo.

REM Arka plan sekmeleri yavaslatilmasin (iki kurs paralel calisirken)
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows "https://ole.unibz.it/course/view.php?id=13232" "https://ole.unibz.it/course/view.php?id=13250"

echo.
echo Chrome acildi.
echo 1^) Hesaba gir
echo 2^) Dersi ac — Next butonunu KENDIN gor
echo 3^) Bu pencereyi kapatma
echo 4^) run.bat veya 2_bot_baglan.bat calistir
echo.
pause
