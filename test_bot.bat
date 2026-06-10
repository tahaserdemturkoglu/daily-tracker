@echo off
chcp 65001 >nul
title Bot Teshis
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo venv yok, pip ile kur...
    py -m venv .venv
    .venv\Scripts\pip install -q python-telegram-bot anthropic
)
.venv\Scripts\python.exe test_bot.py
