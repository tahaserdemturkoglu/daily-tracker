#!/usr/bin/env python3
"""Bot teshis scripti - calistir, ekrandaki ciktiyi paylas"""
import sys, os, json

print("="*50)
print("BOT TESHIS")
print("="*50)

# 1. Python
print(f"\n[1] Python: {sys.version}")

# 2. Config
BASE = os.path.dirname(os.path.abspath(__file__))
cfg_path = os.path.join(BASE, 'config.json')
print(f"\n[2] config.json: {cfg_path}")
if not os.path.exists(cfg_path):
    print("    HATA: config.json bulunamadi!")
    input("Enter..."); sys.exit(1)
with open(cfg_path, encoding='utf-8-sig') as f:
    cfg = json.load(f)
tok = cfg.get('TELEGRAM_TOKEN','')
api = cfg.get('ANTHROPIC_API_KEY','')
print(f"    Token  : {'OK (' + tok[:15] + '...)' if tok else 'EKSIK!'}")
print(f"    API Key: {'OK (' + api[:15] + '...)' if api else 'EKSIK!'}")

# 3. python-telegram-bot
print("\n[3] python-telegram-bot:")
try:
    import telegram
    print(f"    Versiyon: {telegram.__version__}")
    from telegram.ext import Application
    print("    Import: OK")
except ImportError as e:
    print(f"    HATA: {e}")
    print("    Coz: pip install python-telegram-bot")
    input("Enter..."); sys.exit(1)

# 4. Telegram baglanti testi
print("\n[4] Telegram API baglanti testi...")
import urllib.request, urllib.error
try:
    url = f"https://api.telegram.org/bot{tok}/getMe"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    if data.get('ok'):
        bot = data['result']
        print(f"    BASARILI! Bot: @{bot['username']} ({bot['first_name']})")
    else:
        print(f"    HATA: {data}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"    HTTP {e.code}: {body}")
    print("    >>> Token yanlis olmali! config.json'u kontrol et.")
    input("Enter..."); sys.exit(1)
except Exception as e:
    print(f"    HATA: {e}")
    print("    >>> Internet baglantisi yok veya Telegram erisimi engellendi?")

# 5. Anthropic testi
print("\n[5] Anthropic API testi...")
import urllib.request, urllib.error
try:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api, "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        print("    BASARILI!")
except urllib.error.HTTPError as e:
    body = e.read().decode()[:200]
    print(f"    HTTP {e.code}: {body}")
except Exception as e:
    print(f"    HATA: {e}")

print("\n"+"="*50)
print("Teshis tamamlandi. Ciktiyi paylas.")
print("="*50)
input("\nEnter'a bas cik...")
