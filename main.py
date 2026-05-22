"""
main.py — Entry point. Builds the PTB Application, registers all handlers,
          starts Pyrogram MTProto client, and begins polling.

Run with:
  python3 main.py
"""

import asyncio
import logging
import signal
import sys
import time

from telegram import BotCommand, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

from config import BOT_TOKEN
from cookies import init_cookies_from_env, youtube_cookie_status
from uploader import start_pyro_bot, stop_pyro_bot
from utils import launch_health_server, cleanup_worker
import queue_manager
from handlers import (
    cmd_start, cmd_help, cmd_settings, cmd_cookiecheck, cmd_stats,
    settings_callback, download_callback,
    handle_message, error_handler,
)

logger = logging.getLogger(__name__)


def main():
    # ── Startup delay (Render redeploy: wait for old instance to die) ────────
    # Render sends SIGTERM to old container but there's an overlap window.
    # A short sleep ensures the old instance has released the Telegram polling
    # connection before we start, preventing 409 Conflict errors.
    logger.info("Waiting 5s for any previous instance to shut down…")
    time.sleep(5)

    # ── Cookie initialisation ────────────────────────────────────────────────
    init_cookies_from_env()

    cs = youtube_cookie_status()
    if cs["ok"]:
        logger.info("✅ YouTube cookies OK — %d YT lines, SAPISID=%s",
                    cs.get("yt_lines", 0), cs.get("has_sapisid", False))
    else:
        logger.warning("⚠️ YouTube cookies problem: %s", cs["reason"])

    # ── Health server (Render / Railway keep-alive) ──────────────────────────
    launch_health_server()

    # ── Build PTB Application ────────────────────────────────────────────────
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)   # allow handlers to run concurrently
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("cookiecheck", cmd_cookiecheck))
    app.add_handler(CommandHandler("stats",       cmd_stats))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))

    # Text messages (URLs + search)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Global error handler
    app.add_error_handler(error_handler)

    # ── Lifecycle hooks ──────────────────────────────────────────────────────
    async def post_init(application: Application):
        await application.bot.set_my_commands([
            BotCommand("start",       "Welcome & supported platforms"),
            BotCommand("help",        "Help & usage guide"),
            BotCommand("settings",    "Manage preferences"),
            BotCommand("cookiecheck", "Diagnose cookie issues"),
            BotCommand("stats",       "Bot & dependency info"),
        ])
        await start_pyro_bot()           # Pyrogram MTProto (2 GB uploads)
        queue_manager.setup()            # initialise download task queue
        asyncio.create_task(cleanup_worker())

    async def post_shutdown(application: Application):
        await stop_pyro_bot()            # graceful Pyrogram disconnect

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("Bot started — polling")

    # Render sends SIGTERM before killing the container.
    # Handle it so we stop polling cleanly and avoid 409 Conflict on redeploy.
    def _handle_sigterm(*_):
        logger.info("SIGTERM received — shutting down cleanly…")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )


if __name__ == "__main__":
    main()
