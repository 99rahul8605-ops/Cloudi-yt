"""
utils.py — Shared utilities: progress display, error messages,
           background cleanup worker, and health-check HTTP server.
"""

import asyncio
import logging
import re
import subprocess
import sys
import platform
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import yt_dlp as _yt_dlp_module
from telegram.constants import ParseMode

from config import (
    DOWNLOAD_DIR, BOT_START_TIME, cleanup_registry,
    user_settings, _pyro_bot, TELEGRAM_API_ID, TELEGRAM_API_HASH,
)

logger = logging.getLogger(__name__)


# ── Human-readable sizes / times ──────────────────────────────────────────────

def human_size(b: int) -> str:
    if b < 1024 ** 2: return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3: return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def _progress_bar(pct: int, width: int = 16) -> str:
    filled = round(pct * width / 100)
    return "█" * filled + "░" * (width - filled)


def _eta_str(seconds: float) -> str:
    if seconds < 0: return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:  return f"{h}h {m}m"
    if m:  return f"{m}m {s:02d}s"
    return f"{s}s"


def _speed_str(bps: float) -> str:
    if bps <= 0:        return "?"
    if bps >= 1024**3:  return f"{bps/1024**3:.1f} GB/s"
    if bps >= 1024**2:  return f"{bps/1024**2:.1f} MB/s"
    return f"{bps/1024:.0f} KB/s"


# ── Progress text builders ────────────────────────────────────────────────────

def upload_progress_text(filename: str, current: int, total: int, elapsed: float) -> str:
    pct     = min(int(current * 100 / total), 100) if total else 0
    bar     = _progress_bar(pct)
    done    = human_size(current)
    tot     = human_size(total)
    spd     = _speed_str(current / elapsed if elapsed > 0 else 0)
    eta_sec = ((total - current) / (current / elapsed)) if current > 0 and elapsed > 0 else -1
    eta     = _eta_str(eta_sec)
    return "\n".join([
        f"📤 *Uploading* `{filename}`",
        f"`{bar}` {pct}%",
        f"📦 `{done}` / `{tot}`",
        f"⚡ `{spd}`  ⏱ `{eta}`",
    ])


def download_progress_text(label: str, pct_str: str, speed_str: str,
                            eta_str_val: str, downloaded: str, total: str) -> str:
    try:
        pct = int(float(pct_str.replace("%", "").strip()))
    except Exception:
        pct = 0
    bar = _progress_bar(pct)
    tot = f" / `{total}`" if total and total != "?" else ""
    return "\n".join([
        f"⬇️ *Downloading* {label}",
        f"`{bar}` {pct}%",
        f"📦 `{downloaded}`{tot}",
        f"⚡ `{speed_str}`  ⏱ `{eta_str_val}`",
    ])


def build_progress_hook(loop, status_msg, label: str = ""):
    """Build a yt-dlp progress hook that updates a Telegram message."""
    last = [0.0]
    def hook(d):
        if d["status"] != "downloading": return
        now = time.time()
        if now - last[0] < 3: return
        last[0] = now
        pct   = d.get("_percent_str",  "0%").strip()
        speed = d.get("_speed_str",    "?").strip()
        eta   = d.get("_eta_str",      "?").strip()
        down  = d.get("_downloaded_bytes_str", "?").strip()
        total = d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str") or "?"
        total = total.strip() if isinstance(total, str) else "?"
        text  = download_progress_text(label, pct, speed, eta, down, total)
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)
    return hook


# ── Friendly error messages ───────────────────────────────────────────────────

def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    logger.warning("Download/upload error (raw): %s", str(e)[:300])

    if "requested format" in msg:
        return "❌ *Format not available.*\nTry a different quality or use ⭐ Best Available."
    if "no video formats" in msg or "no formats" in msg:
        return "❌ *No downloadable formats found.* The content may be private or region-locked."
    if "only available for registered users who follow" in msg or "follow this account" in msg:
        return (
            "🔒 *Followers-only content.*\n\n"
            "This post is restricted to followers of that account.\n"
            "The bot\'s Instagram account must follow them to download it."
        )
    if "private" in msg or "only available" in msg:
        return "🔒 This content is *private* or restricted."
    if "login" in msg or "sign in" in msg or "not a bot" in msg or "confirm" in msg:
        return (
            "🔒 *Login required or bot-detection triggered.*\n\n"
            "For YouTube: run /cookiecheck\n"
            "For Instagram/Facebook: provide cookies via FB_COOKIES env var\n"
            "For other sites: content may require authentication"
        )
    if "copyright" in msg or "blocked" in msg:
        return "⛔ Blocked due to *copyright restrictions*."
    if "age" in msg:
        return "🔞 *Age-restricted.* Provide cookies from a verified account."
    if "ffmpeg" in msg:
        return "⚙️ *FFmpeg error.* Try a lower quality or ⭐ Best Available."
    if "fragment" in msg or "network" in msg or "connection" in msg:
        return "🌐 *Network error* while downloading. Please retry."
    if "unavailable" in msg or "not available" in msg:
        return "❌ Content *unavailable* — may be region-blocked, removed, or private."
    if "unsupported url" in msg:
        return "❌ *Unsupported URL.* This site or link type is not supported."
    if "rate" in msg or "too many" in msg:
        return "⏳ *Rate limited.* Please wait a minute and try again."
    return f"❌ Download failed:\n`{str(e)[:300]}`"


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_ffmpeg_version() -> str:
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        first_line = (result.stdout or result.stderr).splitlines()[0]
        match = re.search(r"ffmpeg version\s+(\S+)", first_line, re.IGNORECASE)
        return match.group(1) if match else first_line[:60]
    except FileNotFoundError:
        return "❌ Not found in PATH"
    except Exception as exc:
        return f"❌ {exc}"


def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days,  seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    mins,  seconds = divmod(seconds, 60)
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins:  parts.append(f"{mins}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def download_dir_info() -> tuple[int, int]:
    files = list(DOWNLOAD_DIR.iterdir()) if DOWNLOAD_DIR.exists() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    return len(files), total


def get_ytdlp_version() -> str:
    try:
        return _yt_dlp_module.version.__version__
    except Exception:
        return "unknown"


# ── Background cleanup worker ─────────────────────────────────────────────────

async def cleanup_worker():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for path in list(cleanup_registry):
            t = cleanup_registry[path]
            if t != 0.0 and t < now:
                try:
                    Path(path).unlink(missing_ok=True)
                    del cleanup_registry[path]
                    logger.info("Cleaned: %s", path)
                except Exception as exc:
                    logger.warning("Cleanup error %s: %s", path, exc)


# ── Health-check server (Render / Railway keep-alive) ─────────────────────────

import os

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *_): pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    logger.info("Health server :%d", port)
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()

def launch_health_server():
    threading.Thread(target=start_health_server, daemon=True).start()
