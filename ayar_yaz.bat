@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo  AYAR YAZ — secrets.env olusturulur
echo ========================================
echo.
echo Gemini key al: https://aistudio.google.com/apikey
echo.

set /p KEY=GEMINI_API_KEY yapistir: 
set /p URL=Site START_URL yapistir: 

(
  echo # Otomatik yazildi — ayar_yaz.bat
  echo GEMINI_API_KEY=%KEY%
  echo START_URL=%URL%
) > secrets.env

echo.
echo Kaydedildi: %CD%\secrets.env
echo.
type secrets.env
echo.
echo Simdi run.bat cift tikla veya Terminalde: python bot.py
echo.
pause
