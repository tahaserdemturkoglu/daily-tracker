@echo off
chcp 65001 >nul
title Taha Serdem Daily Rapor
cd /d "%~dp0"

echo.
echo  TAHA SERDEM DAILY RAPOR
echo  ========================
echo.

:: Venv kontrol
if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv bulunamadi. Olusturuluyor...
    py -m venv .venv
    if errorlevel 1 (
        echo [HATA] Python bulunamadi. py --version yazarak kontrol et.
        pause & exit /b 1
    )
)

:: Paket kurulumu
echo [*] Paketler kontrol ediliyor...
.venv\Scripts\pip install -q flask python-telegram-bot anthropic 2>nul
echo [OK] Paketler hazir.

:: Calistir
echo.
echo  Dashboard : http://localhost:5000
echo  Bot       : @taha_serdem_daily_rapor_bot
echo.
echo  Bu pencereyi KAPATMA.
echo  Durdurmak icin: Ctrl+C
echo.

start "" "http://localhost:5000"
set DISABLE_EMBEDDED_BOT=1
.venv\Scripts\python.exe app.py
pause
