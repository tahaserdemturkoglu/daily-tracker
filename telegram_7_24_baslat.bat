@echo off
cd /d "%~dp0"
echo Telegram bot 7/24 mod baslatiliyor...
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0telegram_always_on.ps1"
