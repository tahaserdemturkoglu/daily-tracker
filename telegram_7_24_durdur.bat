@echo off
cd /d "%~dp0"
echo stop > "%~dp0telegram_watchdog.stop"
echo Durdurma sinyali gonderildi. Watchdog en gec 20 saniye icinde kapanir.
pause
