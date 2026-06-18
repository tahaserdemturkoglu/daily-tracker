"""Cloud entry point: Flask site + standalone Telegram bot."""
import os
import threading

os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("DISABLE_EMBEDDED_BOT", "1")

from app import app, init_db, PORT, log
from bot import TELEGRAM_TOKEN, main as start_standalone_bot


def main():
    init_db()
    if TELEGRAM_TOKEN:
        threading.Thread(
            target=start_standalone_bot,
            daemon=True,
            name="telegram-bot",
        ).start()
        log.info("Standalone Telegram bot cloud thread started.")
    else:
        log.info("Telegram bot token missing.")
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", PORT)),
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
