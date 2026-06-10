@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [*] Flask yeniden baslatiliyor...
taskkill /F /FI "WINDOWTITLE eq Flask Site" >nul 2>&1
taskkill /F /FI "IMAGENAME eq python.exe" >nul 2>&1
timeout /t 2 /nobreak >nul
set DISABLE_EMBEDDED_BOT=1
start "Flask Site" /MIN "%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
timeout /t 3 /nobreak >nul
echo [OK] Site yenilendi: http://localhost:5000
start "" "http://localhost:5000"
