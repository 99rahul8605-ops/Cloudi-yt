"""
config.py — Environment variables, shared constants, and mutable global state.
All other modules import from here; nothing is defined in two places.
"""

import os
import time
import logging
from pathlib import Path
from pyrogram import Client as PyroClient

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Telegram credentials ──────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["BOT_TOKEN"]
TELEGRAM_API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
OWNER_ID          = int(os.environ.get("OWNER_ID", "0")) or None  # Owner user ID for restricted commands

# ── Paths ─────────────────────────────────────────────────────────────────────
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_FILE    = "cookies.txt"    # YouTube cookies (Netscape format)
FB_COOKIES_FILE = "fb_cookies.txt" # Facebook/Instagram cookies (optional)
IG_COOKIES_FILE = "ig_cookies.txt" # Separate Instagram cookies (optional)

# ── Proxy ─────────────────────────────────────────────────────────────────────
# Set YTDL_PROXY=http://host:port in environment variables.
# Required on some hosts (e.g. Render) to bypass YouTube/Instagram bot-detection.
YTDL_PROXY = os.environ.get("YTDL_PROXY", "")

# ── Rotating user-agents ──────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

# ── User settings ─────────────────────────────────────────────────────────────
DEFAULT_SETTINGS: dict = {"quality": "720p", "mode": "manual", "cleanup_minutes": 10}
user_settings:    dict[int, dict]  = {}
cleanup_registry: dict[str, float] = {}

def get_settings(uid: int) -> dict:
    if uid not in user_settings:
        user_settings[uid] = DEFAULT_SETTINGS.copy()
    return user_settings[uid]

def register_for_cleanup(path: str, minutes: int) -> None:
    cleanup_registry[path] = 0.0 if minutes == 0 else time.time() + minutes * 60

# ── Bot start time (for /stats uptime) ───────────────────────────────────────
BOT_START_TIME = time.time()

# ── Pyrogram MTProto client (module-level singleton) ─────────────────────────
# Started in post_init, stopped in post_shutdown.
_pyro_bot: "PyroClient | None" = None
