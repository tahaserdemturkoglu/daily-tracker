@echo off
chcp 65001 >nul
title Taha - Daily Rapor (Bot + Site)
cd /d "%~dp0"

echo.
echo  TAHA SERDEM DAILY RAPOR
echo  ========================
echo  Bot  : @taha_serdem_daily_rapor_bot
echo  Site : http://localhost:5000
echo  Durdurmak: Ctrl+C (bu pencere)
echo.

:: Venv
if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv olusturuluyor...
    py -m venv .venv
    if errorlevel 1 ( echo [HATA] Python bulunamadi. & pause & exit /b 1 )
)

:: Paketler
echo [*] Paketler kontrol ediliyor...
.venv\Scripts\pip install -q "python-telegram-bot>=20.0" anthropic flask 2>nul
echo [OK] Hazir.

:: Cakisan surecler temizle
echo [*] Onceki surecler temizleniyor...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM python3.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul

:: Flask siteyi ARKA PLANDA baslat
set DISABLE_EMBEDDED_BOT=1
echo [*] Site arka planda baslatiliyor...
start "Flask Site" /MIN .venv\Scripts\python.exe app.py
timeout /t 4 /nobreak >nul
echo [OK] Site: http://localhost:5000
start "" "http://localhost:5000"

:: Bot basalt (on planda - bu pencerede calisir)
echo [*] Bot baslatiliyor...
echo.
.venv\Scripts\python.exe bot.py
pause
