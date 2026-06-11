"""Railway entry point — Flask + Telegram bot birlikte baslatir"""
import os, threading, asyncio, sys

# Windows asyncio fix (local dev icin)
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def run_flask():
    os.environ['DISABLE_EMBEDDED_BOT'] = '1'
    from app import app
    port = int(os.environ.get('PORT', 5000))
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def run_bot():
    import importlib.util, asyncio
    spec = importlib.util.spec_from_file_location("bot", os.path.join(os.path.dirname(__file__), "bot.py"))
    bot_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot_module)
    asyncio.run(bot_module._run_bot())

if __name__ == '__main__':
    # Flask arka planda
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    # Bot on planda
    run_bot()
