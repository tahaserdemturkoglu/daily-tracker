"""Cloud entry point: Flask site + Telegram Bot (webhook mode)."""
import os
import json
import urllib.request

os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("DISABLE_EMBEDDED_BOT", "1")

from app import app, init_db, PORT, log
from bot import TELEGRAM_TOKEN


def register_webhook():
    """Telegram'a Railway URL'ini webhook olarak kaydet."""
    # Railway public URL'i env var'dan al
    domain = (
        os.environ.get('RAILWAY_PUBLIC_DOMAIN') or
        os.environ.get('RAILWAY_STATIC_URL', '').replace('https://', '').replace('http://', '')
    )
    if not domain or not TELEGRAM_TOKEN:
        log.warning("Webhook kaydedilemedi: domain=%s token=%s", domain, bool(TELEGRAM_TOKEN))
        return

    webhook_url = f"https://{domain}/telegram_webhook"
    pay