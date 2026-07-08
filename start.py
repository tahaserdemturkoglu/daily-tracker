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
    payload = json.dumps({
        'url': webhook_url,
        'drop_pending_updates': True,
        'allowed_updates': ['message', 'callback_query'],
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        if result.get('ok'):
            log.info("Webhook kayıt OK: %s", webhook_url)
        else:
            log.error("Webhook kayıt HATA: %s", result)
    except Exception as e:
        log.error("Webhook kayıt exception: %s", e)


def pull_latest_template():
    """GitHub'dan index.html çek, Railway volume'daki eski dosyayı güncelle."""
    try:
        url = 'https://raw.githubusercontent.com/tahaserdemturkoglu/daily-tracker/main/templates/index.html'
        with urllib.request.urlopen(url, timeout=30) as resp:
            content = resp.read().decode('utf-8')
        base_dir = os.path.dirname(os.path.abspath(__file__))
        tpl_path = os.path.join(base_dir, 'templates', 'index.html')
        with open(tpl_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log.info('Template auto-pulled from GitHub: %d bytes', len(content))
    except Exception as e:
        log.warning('Template auto-pull failed (volume may have old file): %s', e)


def main():
    init_db()
    pull_latest_template()
    register_webhook()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", PORT)),
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
