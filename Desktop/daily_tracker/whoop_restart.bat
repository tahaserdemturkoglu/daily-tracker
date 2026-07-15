@echo off
chcp 65001 >nul
title daily-tracker local restart
cd /d "%~dp0"

echo [*] 5057 portundaki eski sunucu kapatiliyor...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5057" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

echo [*] Sunucu yeniden baslatiliyor (port 5057)...
set PORT=5057
set DISABLE_EMBEDDED_BOT=1
start "daily-tracker-local" .venv\Scripts\python.exe app.py
echo [OK] Baslatildi: http://localhost:5057
timeout /t 3 >nul
