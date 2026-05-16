"""
Advanced Telegram YouTube Downloader Bot
python-telegram-bot v21 (handlers) + Pyrogram (MTProto upload) | yt-dlp | FFmpeg

Architecture:
  • python-telegram-bot  → all commands, callbacks, inline keyboards, polling
  • Pyrogram (MTProto)   → video/audio/file uploads (no 50 MB Bot API cap,
                           direct DC streaming, real upload progress)
  • yt-dlp              → download to disk, then stream-upload via Pyrogram

YouTube bypass strategy (ordered by reliability):
  1. cookies.txt auto-detected + validated on startup
  2. /cookiecheck command – shows cookie status + first valid line
  3. ios + web + mweb + tv_embedded + android_music client chain
  4. age_gate bypass via embed extraction
  5. Rotating User-Agents
  6. Extractor / fragment retries + pacing
  7. compat_opts workarounds

Required env vars:
  BOT_TOKEN   – Telegram Bot token (from @BotFather)
  API_ID      – Telegram API id   (from https://my.telegram.org)
  API_HASH    – Telegram API hash (from https://my.telegram.org)
"""

import os, asyncio, time, logging, re, threading, random, urllib.request, sys, platform, subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from pyrogram import Client as PyroClient

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError
import yt_dlp as _yt_dlp_module

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
API_ID       = int(os.environ["API_ID"])
API_HASH     = os.environ["API_HASH"]
DOWNLOAD_DIR = Path("downloads")
COOKIES_FILE = "cookies.txt"
DOWNLOAD_DIR.mkdir(exist_ok=True)
BOT_START_TIME = time.time()

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

DEFAULT_SETTINGS = {"quality": "720p", "mode": "manual", "cleanup_minutes": 10}
user_settings:    dict[int, dict]  = {}
cleanup_registry: dict[str, float] = {}

# ── Pyrogram MTProto client (bot mode) ───────────────────────────────────────
# Uses bot token — no user session file needed. in_memory=True = no disk session.
pyro: PyroClient = PyroClient(
    name="ytdl_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


# ═════════════════════════════════════════════════════════════════════════════
#  COOKIE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def cookie_status() -> dict:
    path = Path(COOKIES_FILE)
    if not path.exists():
        return {"ok": False, "reason": "File not found", "path": str(path.resolve())}
    size = path.stat().st_size
    if size < 100:
        return {"ok": False, "reason": f"File too small ({size} bytes) – probably empty/placeholder",
                "path": str(path.resolve()), "size": size}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as e:
        return {"ok": False, "reason": f"Cannot read file: {e}", "path": str(path.resolve())}

    real_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    yt_lines   = [l for l in real_lines if "youtube.com" in l or "google.com" in l]

    if not real_lines:
        return {"ok": False, "reason": "File has no cookie data (only comments/blank lines)",
                "path": str(path.resolve())}
    if not yt_lines:
        return {"ok": False,
                "reason": "No youtube.com or google.com cookies found – "
                          "make sure you export while on youtube.com",
                "path": str(path.resolve()), "total_lines": len(real_lines)}

    has_sapisid = any("SAPISID" in l for l in yt_lines)
    has_sid     = any("\tSID\t" in l or "\t__Secure-1PSID\t" in l for l in yt_lines)
    sample      = yt_lines[0][:120] if yt_lines else ""

    return {
        "ok": True, "path": str(path.resolve()), "size": size,
        "total": len(real_lines), "yt_lines": len(yt_lines),
        "has_sapisid": has_sapisid, "has_sid": has_sid, "sample": sample,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  YT-DLP OPTIONS
# ═════════════════════════════════════════════════════════════════════════════

def ydl_opts_base(use_cookies: bool = True) -> dict:
    """
    Used for metadata/info extraction.
    tv_embedded first — best at bypassing restrictions when reading info.
    player_skip NOT set so the full format list is returned.
    """
    opts: dict = {
        "quiet":       True,
        "no_warnings": True,
        "noplaylist":  True,
        "outtmpl":     str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "format_sort": ["res", "ext:mp4:m4a", "codec:h264:aac", "size"],
        "retries":             10,
        "fragment_retries":    10,
        "extractor_retries":   5,
        "file_access_retries": 5,
        "socket_timeout":      30,
        "sleep_interval_requests": 1,
        "sleep_interval":          2,
        "max_sleep_interval":      5,
        "http_headers": {
            "User-Agent":      random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "DNT":             "1",
            "Sec-Fetch-Mode":  "navigate",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "mweb", "android_music", "ios", "web"],
            }
        },
        "compat_opts": {"no-youtube-unavailable-videos"},
    }
    if use_cookies:
        cs = cookie_status()
        if cs["ok"]:
            opts["cookiefile"] = COOKIES_FILE
            logger.info("cookies.txt loaded (%d YT lines)", cs.get("yt_lines", 0))
        else:
            logger.warning("cookies.txt problem: %s", cs["reason"])
    return opts


def ydl_opts_download() -> dict:
    """
    Used for actual file downloads.
    ios/web first — they serve full resolution (720p/1080p).
    tv_embedded pushed to end — it caps at 360p.
    No player_skip — skipping configs strips the format list and causes 360p fallback.
    """
    opts = ydl_opts_base()
    opts["extractor_args"]["youtube"]["player_client"] = [
        "ios",           # Full resolution, minimal bot-detection
        "web",           # Standard web — all resolutions
        "mweb",          # Mobile web fallback
        "tv_embedded",   # Age-gate bypass only (360p max)
        "android_music", # Final fallback
    ]
    return opts


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_settings(uid: int) -> dict:
    if uid not in user_settings:
        user_settings[uid] = DEFAULT_SETTINGS.copy()
    return user_settings[uid]


def quality_to_format(q: str) -> str:
    h = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080}.get(q)
    if h is None:
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=webm]"
            "/bestvideo[ext=webm]+bestaudio[ext=webm]"
            "/bestvideo[ext=webm]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio/best"
        )
    h_up = h + 360
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=webm]"
        f"/bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]"
        f"/bestvideo[height<={h}][ext=webm]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}]+bestaudio"
        f"/best[height<={h}][ext=mp4]"
        f"/best[height<={h}]"
        f"/bestvideo[height<={h_up}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h_up}][ext=mp4]+bestaudio[ext=webm]"
        f"/bestvideo[height<={h_up}][ext=webm]+bestaudio[ext=webm]"
        f"/bestvideo[height<={h_up}]+bestaudio"
        f"/best[height<={h_up}]"
        "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[ext=mp4]+bestaudio[ext=webm]"
        "/bestvideo[ext=webm]+bestaudio[ext=webm]"
        "/bestvideo+bestaudio/best"
    )


def register_for_cleanup(path: str, minutes: int):
    cleanup_registry[path] = 0.0 if minutes == 0 else time.time() + minutes * 60


def is_youtube_url(text: str) -> bool:
    return bool(re.match(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", text.strip()))


def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "sign in" in msg or "not a bot" in msg or "confirm" in msg or "cookie" in msg:
        return (
            "🔒 *YouTube is blocking this video.*\n\n"
            "Your cookies.txt may be expired or missing.\n\n"
            "📋 *Run /cookiecheck to diagnose.*\n\n"
            "*Common fixes:*\n"
            "• Re-export cookies while logged into YouTube\n"
            "• Export from `youtube.com` (not google.com)\n"
            "• Use *'Get cookies.txt LOCALLY'* extension\n"
            "• Disable incognito mode\n"
            "• Try a different Google account"
        )
    if "private" in msg:
        return "🔒 This video is *private*."
    if "unavailable" in msg or "not available" in msg:
        return "❌ Video *unavailable* — may be region-blocked or removed."
    if "age" in msg:
        return "🔞 *Age-restricted.* Provide cookies from a verified account."
    if "copyright" in msg or "blocked" in msg:
        return "⛔ Blocked due to *copyright restrictions*."
    if "ffmpeg" in msg:
        return "⚙️ *FFmpeg error.* Try a lower quality."
    if "fragment" in msg:
        return "🌐 *Network error* downloading fragments. Please retry."
    if "requested format" in msg:
        return "❌ *Requested format not available.* Retrying with best available…"
    if "no video formats" in msg:
        return "❌ *No downloadable formats found* for this video."
    return f"❌ Download failed:\n`{str(e)[:400]}`"


def fetch_thumb(url: str | None) -> bytes | None:
    """Download thumbnail bytes, or return None on failure."""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read()
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  CORE ASYNC WRAPPERS (yt-dlp)
# ═════════════════════════════════════════════════════════════════════════════

async def extract_info(url: str, download: bool = False,
                       extra_opts: dict | None = None) -> dict:
    opts = ydl_opts_base()
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)
    return await loop.run_in_executor(None, _run)


async def do_download(url: str, extra_opts: dict, progress_cb) -> dict:
    opts = ydl_opts_download()
    opts.update(extra_opts)
    opts["progress_hooks"] = [progress_cb]
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    return await loop.run_in_executor(None, _run)


def build_dl_progress_hook(loop, status_msg):
    """yt-dlp download progress → edit status message (throttled to 3 s)."""
    last = [0.0]
    def hook(d):
        if d["status"] != "downloading":
            return
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        pct   = d.get("_percent_str",  "?%").strip()
        speed = d.get("_speed_str",    "?").strip()
        eta   = d.get("_eta_str",      "?").strip()
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(
                f"⬇️ *Downloading…*\n`{pct}` | 🚀 `{speed}` | ⏱ ETA `{eta}`",
                parse_mode=ParseMode.MARKDOWN,
            ), loop)
    return hook


# ═════════════════════════════════════════════════════════════════════════════
#  PYROGRAM MTProto UPLOAD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def make_upload_progress_cb(loop, status_msg, label: str):
    """
    Pyrogram upload progress callback: (current_bytes, total_bytes).
    Throttled to one edit per 4 s to avoid flood-wait errors.
    """
    last      = [0.0]
    last_curr = [0]
    start     = [time.time()]

    def _cb(current: int, total: int):
        now = time.time()
        if now - last[0] < 4:
            return
        elapsed = now - start[0] + 0.001
        speed   = (current - last_curr[0]) / (now - last[0] + 0.001)
        last[0]      = now
        last_curr[0] = current

        pct     = f"{current / total * 100:.1f}%" if total else "?"
        spd_str = (
            f"{speed / (1024*1024):.1f} MB/s" if speed > 1024*1024
            else f"{speed / 1024:.1f} KB/s"
        )
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(
                f"{label}\n`{pct}` | 🚀 `{spd_str}`",
                parse_mode=ParseMode.MARKDOWN,
            ), loop)

    return _cb


async def pyro_send_video(chat_id: int, filepath: str, caption: str,
                          info: dict, status_msg, loop) -> None:
    """
    Stream-upload a video via Pyrogram MTProto.
    • No 50 MB Bot API size cap
    • Real upload progress displayed in status message
    • YouTube thumbnail attached as cover art
    """
    # Save thumbnail to a temp file (Pyrogram needs a path, not bytes)
    thumb_data = fetch_thumb(info.get("thumbnail"))
    thumb_path = None
    if thumb_data:
        thumb_path = str(DOWNLOAD_DIR / f"_thumb_{info.get('id', 'x')}.jpg")
        Path(thumb_path).write_bytes(thumb_data)

    progress_cb = make_upload_progress_cb(loop, status_msg, "📤 *Uploading video…*")

    try:
        await pyro.send_video(
            chat_id=chat_id,
            video=filepath,
            caption=caption,
            duration=info.get("duration") or 0,
            width=info.get("width") or 0,
            height=info.get("height") or 0,
            thumb=thumb_path,
            supports_streaming=True,
            progress=progress_cb,
        )
    finally:
        if thumb_path:
            Path(thumb_path).unlink(missing_ok=True)


async def pyro_send_audio(chat_id: int, filepath: str, caption: str,
                          info: dict, status_msg, loop) -> None:
    """Stream-upload an MP3 via Pyrogram MTProto with thumbnail."""
    thumb_data = fetch_thumb(info.get("thumbnail"))
    thumb_path = None
    if thumb_data:
        thumb_path = str(DOWNLOAD_DIR / f"_thumb_{info.get('id', 'x')}.jpg")
        Path(thumb_path).write_bytes(thumb_data)

    progress_cb = make_upload_progress_cb(loop, status_msg, "📤 *Uploading audio…*")

    try:
        await pyro.send_audio(
            chat_id=chat_id,
            audio=filepath,
            caption=caption,
            duration=info.get("duration") or 0,
            performer=info.get("uploader") or "",
            title=info.get("title") or "",
            thumb=thumb_path,
            progress=progress_cb,
        )
    finally:
        if thumb_path:
            Path(thumb_path).unlink(missing_ok=True)


async def pyro_send_photo(chat_id: int, filepath: str, caption: str) -> None:
    """Upload a photo via Pyrogram MTProto."""
    await pyro.send_photo(chat_id=chat_id, photo=filepath, caption=caption)


# ═════════════════════════════════════════════════════════════════════════════
#  BACKGROUND CLEANUP
# ═════════════════════════════════════════════════════════════════════════════

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


# ── Health server ─────────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *_): pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    logger.info("Health server :%d", port)
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ═════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to YT Downloader Bot!*\n\n"
        "Send me:\n"
        "• A *YouTube URL* → video / audio / thumbnail\n"
        "• A *song or video name* → search (top 5 results)\n\n"
        "⚙️ /settings – Preferences\n"
        "🍪 /cookiecheck – Diagnose cookie issues\n"
        "❓ /help – This message",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_cookiecheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cs = cookie_status()
    if not cs["ok"]:
        msg = (
            "🍪 *Cookie Check — ❌ PROBLEM FOUND*\n\n"
            f"📁 Path: `{cs.get('path', '?')}`\n"
            f"❗ Issue: *{cs['reason']}*\n\n"
            "*How to fix:*\n"
            "1. Open Chrome/Firefox and go to `youtube.com`\n"
            "2. Make sure you're *logged in* to Google\n"
            "3. Install: *'Get cookies.txt LOCALLY'* extension\n"
            "4. Click extension → *Export as* → save `cookies.txt`\n"
            "5. Replace your `cookies.txt` file and redeploy\n\n"
            "⚠️ *Do NOT export in incognito mode*\n"
            "⚠️ *Export from youtube.com, not google.com*"
        )
    else:
        sapisid_status = "✅" if cs.get("has_sapisid") else "⚠️ Missing"
        sid_status     = "✅" if cs.get("has_sid")     else "⚠️ Missing"
        msg = (
            "🍪 *Cookie Check — ✅ File looks valid*\n\n"
            f"📁 Path: `{cs['path']}`\n"
            f"📦 Size: `{cs['size']} bytes`\n"
            f"🔢 Total cookie lines: `{cs['total']}`\n"
            f"🎯 YouTube/Google lines: `{cs['yt_lines']}`\n"
            f"🔑 SAPISID: {sapisid_status}\n"
            f"🔑 SID: {sid_status}\n\n"
            f"📄 Sample line:\n`{cs.get('sample', 'N/A')[:100]}`\n\n"
        )
        if not cs.get("has_sapisid") or not cs.get("has_sid"):
            msg += "⚠️ *Missing critical auth cookies.* Re-export while fully logged into YouTube."
        else:
            msg += "✅ Cookies look complete. If downloads still fail, re-export from a fresh session."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


def _get_ffmpeg_version() -> str:
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        first_line = (result.stdout or result.stderr).splitlines()[0]
        match = re.search(r"ffmpeg version\s+(\S+)", first_line, re.IGNORECASE)
        return match.group(1) if match else first_line[:60]
    except FileNotFoundError: return "❌ Not found in PATH"
    except Exception as exc:  return f"❌ {exc}"

def _format_uptime(seconds: float) -> str:
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

def _download_dir_info() -> tuple[int, int]:
    files = list(DOWNLOAD_DIR.iterdir()) if DOWNLOAD_DIR.exists() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    return len(files), total


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:    ytdlp_ver = _yt_dlp_module.version.__version__
    except: ytdlp_ver = "unknown"
    try:    import pyrogram; pyro_ver = pyrogram.__version__
    except: pyro_ver = "unknown"

    ffmpeg_ver            = _get_ffmpeg_version()
    python_ver            = sys.version.split()[0]
    os_info               = f"{platform.system()} {platform.release()}"
    uptime_str            = _format_uptime(time.time() - BOT_START_TIME)
    file_count, dir_bytes = _download_dir_info()
    dir_mb                = dir_bytes / (1024 * 1024)
    active_users          = len(user_settings)
    queued_files          = len(cleanup_registry)
    cs                    = cookie_status()
    cookie_icon           = "✅" if cs["ok"] else "❌"
    cookie_label          = (
        f"{cs.get('yt_lines', 0)} YT cookies, SAPISID={'✅' if cs.get('has_sapisid') else '⚠️'}"
        if cs["ok"] else cs["reason"]
    )

    msg = (
        "📊 *Bot Statistics*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔧 *Dependencies*\n"
        f"  • yt-dlp:   `{ytdlp_ver}`\n"
        f"  • FFmpeg:   `{ffmpeg_ver}`\n"
        f"  • Python:   `{python_ver}`\n"
        f"  • Pyrogram: `{pyro_ver}`\n"
        f"  • OS:       `{os_info}`\n\n"
        "⏱ *Runtime*\n"
        f"  • Uptime: `{uptime_str}`\n\n"
        "👥 *Usage*\n"
        f"  • Active user profiles:  `{active_users}`\n"
        f"  • Files pending cleanup: `{queued_files}`\n\n"
        "💾 *Download Folder*\n"
        f"  • Files: `{file_count}`\n"
        f"  • Size:  `{dir_mb:.2f} MB`\n\n"
        "🍪 *Cookies*\n"
        f"  • Status: {cookie_icon} `{cookie_label}`\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════════════════════

def settings_keyboard(uid: int) -> InlineKeyboardMarkup:
    s = get_settings(uid)
    mode_lbl  = "Fixed ✅" if s["mode"] == "fixed" else "Manual 🎛"
    timer_lbl = "♾ Never"  if s["cleanup_minutes"] == 0 else f"{s['cleanup_minutes']} min"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Default Quality: {s['quality'].upper()}", callback_data="s:quality")],
        [InlineKeyboardButton(f"🔁 Download Mode: {mode_lbl}",               callback_data="s:mode")],
        [InlineKeyboardButton(f"🧹 Cleanup Timer: {timer_lbl}",              callback_data="s:cleanup")],
        [InlineKeyboardButton("❌ Close",                                     callback_data="s:close")],
    ])

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("⚙️ *Your Settings*\nTap an option to change it:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(uid))

async def settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; uid = q.from_user.id; await q.answer()
    parts = q.data.split(":")

    if parts[1] == "close":
        await q.message.delete(); return
    if parts[1] == "back":
        await q.message.edit_text("⚙️ *Your Settings*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(uid)); return

    if parts[1] == "quality" and len(parts) == 2:
        await q.message.edit_text("🎬 *Select Default Video Quality:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("360p",  callback_data="s:set:quality:360p"),
                 InlineKeyboardButton("480p",  callback_data="s:set:quality:480p")],
                [InlineKeyboardButton("720p",  callback_data="s:set:quality:720p"),
                 InlineKeyboardButton("1080p", callback_data="s:set:quality:1080p")],
                [InlineKeyboardButton("⭐ Best Available", callback_data="s:set:quality:best")],
                [InlineKeyboardButton("⬅️ Back",           callback_data="s:back")],
            ])); return

    if parts[1] == "mode" and len(parts) == 2:
        await q.message.edit_text(
            "🔁 *Download Mode:*\n\n"
            "• *Fixed* – always use default quality\n"
            "• *Manual* – choose quality per download",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Fixed Quality",    callback_data="s:set:mode:fixed")],
                [InlineKeyboardButton("🎛 Manual Selection", callback_data="s:set:mode:manual")],
                [InlineKeyboardButton("⬅️ Back",             callback_data="s:back")],
            ])); return

    if parts[1] == "cleanup" and len(parts) == 2:
        await q.message.edit_text(
            "🧹 *Auto-Cleanup Timer:*\nFiles deleted after this delay.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("5 min",   callback_data="s:set:cleanup:5"),
                 InlineKeyboardButton("10 min",  callback_data="s:set:cleanup:10")],
                [InlineKeyboardButton("15 min",  callback_data="s:set:cleanup:15"),
                 InlineKeyboardButton("30 min",  callback_data="s:set:cleanup:30")],
                [InlineKeyboardButton("♾ Never", callback_data="s:set:cleanup:0")],
                [InlineKeyboardButton("⬅️ Back",  callback_data="s:back")],
            ])); return

    if parts[1] == "set" and len(parts) == 4:
        key, value = parts[2], parts[3]
        s = get_settings(uid)
        if key == "quality":   s["quality"] = value
        elif key == "mode":    s["mode"] = value
        elif key == "cleanup": s["cleanup_minutes"] = int(value)
        await q.message.edit_text("✅ *Setting saved!*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(uid))


# ═════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ═════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_youtube_url(text):
        await handle_youtube_url(update, ctx, text)
    else:
        await handle_search(update, ctx, text)


async def handle_youtube_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("🔍 *Fetching video info…*", parse_mode=ParseMode.MARKDOWN)
    try:
        info = await extract_info(url)
    except (DownloadError, ExtractorError) as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return
    except Exception as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return

    title    = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    dur_str  = f"{duration // 60}m {duration % 60}s" if duration else "?"
    ctx.user_data["url"]  = url
    ctx.user_data["info"] = info

    await msg.edit_text(
        f"📹 *{title}*\n⏱ `{dur_str}`\n\nWhat would you like?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Video",     callback_data="dl:video")],
            [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")],
            [InlineKeyboardButton("❌ Cancel",    callback_data="dl:cancel")],
        ]),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD CALLBACKS
# ═════════════════════════════════════════════════════════════════════════════

async def download_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; uid = q.from_user.id; await q.answer()
    parts = q.data.split(":")
    action = parts[1]

    if action == "cancel":
        await q.message.edit_text("❌ Download cancelled."); return
    if action == "thumb":
        await do_thumbnail(q, ctx, uid); return
    if action == "audio":
        await do_audio(q, ctx, uid); return
    if action == "video":
        s = get_settings(uid)
        if s["mode"] == "fixed":
            await do_video(q, ctx, uid, s["quality"])
        else:
            await show_quality_menu(q, ctx)
        return
    if action == "quality" and len(parts) == 3:
        await do_video(q, ctx, uid, parts[2]); return
    if action == "search" and len(parts) == 3:
        results = ctx.user_data.get("search_results", [])
        idx = int(parts[2])
        if idx < len(results):
            entry = results[idx]
            ctx.user_data["url"]  = entry.get("webpage_url") or entry.get("url", "")
            ctx.user_data["info"] = entry
            await q.message.edit_text(
                f"🎵 *{entry.get('title', '?')}*\n\nChoose download type:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Video",     callback_data="dl:video")],
                    [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
                    [InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")],
                    [InlineKeyboardButton("❌ Cancel",    callback_data="dl:cancel")],
                ]),
            )


async def show_quality_menu(q, ctx):
    info    = ctx.user_data.get("info", {})
    formats = info.get("formats", [])

    detected = set(
        int(f["height"]) for f in formats
        if f.get("height")
        and isinstance(f["height"], (int, float))
        and int(f["height"]) > 0
        and f.get("vcodec") not in (None, "none")
    )

    standard = [360, 480, 720, 1080]
    rows, row = [], []
    for h in standard:
        label = f"✅ {h}p" if (detected and h in detected) else f"{h}p"
        row.append(InlineKeyboardButton(label, callback_data=f"dl:quality:{h}p"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⭐ Best Available", callback_data="dl:quality:best")])
    rows.append([InlineKeyboardButton("❌ Cancel",         callback_data="dl:cancel")])

    note = "\n_ℹ️ Format list unavailable — all qualities will be attempted._" if not detected else ""
    await q.message.edit_text(
        f"🎬 *Select video quality:*{note}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ─── Video ────────────────────────────────────────────────────────────────────
async def do_video(q, ctx, uid: int, quality: str):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored. Please resend the link."); return

    status = await q.message.edit_text(f"⬇️ *Downloading ({quality})…*", parse_mode=ParseMode.MARKDOWN)
    loop   = asyncio.get_event_loop()
    hook   = build_dl_progress_hook(loop, status)

    try:
        info = await do_download(url, {"format": quality_to_format(quality)}, hook)
    except (DownloadError, ExtractorError) as e:
        err_str = str(e).lower()
        if any(kw in err_str for kw in ("requested format", "not available", "no video formats")):
            logger.warning("Format unavailable for %s @ %s — retrying with best", url, quality)
            await status.edit_text(
                f"⚠️ *{quality} not available — retrying with best quality…*",
                parse_mode=ParseMode.MARKDOWN,
            )
            try:
                info = await do_download(url, {
                    "format": (
                        "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                        "/bestvideo[ext=mp4]+bestaudio[ext=webm]"
                        "/bestvideo[ext=webm]+bestaudio[ext=webm]"
                        "/bestvideo+bestaudio/best"
                    ),
                }, hook)
            except Exception as e2:
                await status.edit_text(friendly_error(e2), parse_mode=ParseMode.MARKDOWN); return
        else:
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return
    except Exception as e:
        await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return

    vid_id = info.get("id", "")
    files  = (
        list(DOWNLOAD_DIR.glob(f"{vid_id}.mp4"))
        or list(DOWNLOAD_DIR.glob(f"{vid_id}.mkv"))
        or list(DOWNLOAD_DIR.glob(f"{vid_id}.webm"))
        or list(DOWNLOAD_DIR.glob(f"{vid_id}.*"))
    )
    if not files:
        await status.edit_text("❌ File not found after download."); return

    filepath = str(files[0])
    await status.edit_text("📤 *Uploading via MTProto…*", parse_mode=ParseMode.MARKDOWN)

    try:
        await pyro_send_video(
            chat_id=q.message.chat_id,
            filepath=filepath,
            caption=f"🎬 *{info.get('title', '')}* [{quality}]",
            info=info,
            status_msg=status,
            loop=loop,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return

    register_for_cleanup(filepath, get_settings(uid)["cleanup_minutes"])


# ─── Audio ────────────────────────────────────────────────────────────────────
async def do_audio(q, ctx, uid: int):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored."); return

    status = await q.message.edit_text("⬇️ *Extracting audio…*", parse_mode=ParseMode.MARKDOWN)
    loop   = asyncio.get_event_loop()
    hook   = build_dl_progress_hook(loop, status)

    try:
        info = await do_download(url, {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": "192"}],
        }, hook)
    except (DownloadError, ExtractorError) as e:
        await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return
    except Exception as e:
        await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return

    vid_id = info.get("id", "")
    files  = list(DOWNLOAD_DIR.glob(f"{vid_id}.mp3")) or list(DOWNLOAD_DIR.glob(f"{vid_id}.*"))
    if not files:
        await status.edit_text("❌ Audio file not found."); return

    filepath = str(files[0])
    await status.edit_text("📤 *Uploading audio via MTProto…*", parse_mode=ParseMode.MARKDOWN)

    try:
        await pyro_send_audio(
            chat_id=q.message.chat_id,
            filepath=filepath,
            caption=f"🎵 *{info.get('title', '')}*",
            info=info,
            status_msg=status,
            loop=loop,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return

    register_for_cleanup(filepath, get_settings(uid)["cleanup_minutes"])


# ─── Thumbnail ────────────────────────────────────────────────────────────────
async def do_thumbnail(q, ctx, uid: int):
    info      = ctx.user_data.get("info", {})
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        await q.message.edit_text("❌ No thumbnail found."); return

    status  = await q.message.edit_text("🖼 *Downloading thumbnail…*", parse_mode=ParseMode.MARKDOWN)
    outpath = DOWNLOAD_DIR / f"{info.get('id', 'thumb')}_thumb.jpg"

    thumb_data = fetch_thumb(thumb_url)
    if not thumb_data:
        await status.edit_text("❌ Thumbnail fetch failed."); return
    outpath.write_bytes(thumb_data)

    try:
        await pyro_send_photo(
            chat_id=q.message.chat_id,
            filepath=str(outpath),
            caption=f"🖼 *{info.get('title', '')}*",
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return

    register_for_cleanup(str(outpath), get_settings(uid)["cleanup_minutes"])


# ─── Search ───────────────────────────────────────────────────────────────────
async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text(f"🔎 Searching: *{query}*…", parse_mode=ParseMode.MARKDOWN)
    try:
        results_info = await extract_info(f"ytsearch5:{query}", download=False,
            extra_opts={"extract_flat": True})
    except Exception as e:
        await msg.edit_text(f"❌ Search failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return

    entries = results_info.get("entries", [])
    if not entries:
        await msg.edit_text("😕 No results found."); return

    ctx.user_data["search_results"] = entries
    buttons = []
    for i, entry in enumerate(entries[:5]):
        title   = entry.get("title", "Unknown")[:52]
        dur     = entry.get("duration", 0)
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
        buttons.append([InlineKeyboardButton(
            f"{i+1}. {title} [{dur_str}]", callback_data=f"dl:search:{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await msg.edit_text("🎵 *Top results — tap to select:*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


# ── Global error handler ──────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=ctx.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("⚠️ Unexpected error. Please try again.")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    cs = cookie_status()
    if cs["ok"]:
        logger.info("✅ cookies.txt OK — %d YouTube/Google lines, SAPISID=%s",
                    cs.get("yt_lines", 0), cs.get("has_sapisid", False))
    else:
        logger.warning("⚠️ cookies.txt problem: %s", cs["reason"])
        logger.warning("   Bot will try client fallback chain (ios/web/tv_embedded)")

    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("cookiecheck", cmd_cookiecheck))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    async def post_init(application: Application):
        await application.bot.set_my_commands([
            BotCommand("start",       "Welcome message"),
            BotCommand("help",        "Help & usage"),
            BotCommand("settings",    "Manage preferences"),
            BotCommand("cookiecheck", "Diagnose cookie issues"),
            BotCommand("stats",       "Bot & dependency info"),
        ])
        # Start Pyrogram MTProto client alongside PTB
        await pyro.start()
        logger.info("✅ Pyrogram MTProto client started")
        asyncio.create_task(cleanup_worker())

    async def post_shutdown(application: Application):
        await pyro.stop()
        logger.info("Pyrogram MTProto client stopped")

    app.post_init     = post_init
    app.post_shutdown = post_shutdown

    logger.info("Bot started — polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
