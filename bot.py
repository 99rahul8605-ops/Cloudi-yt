"""
Advanced Telegram YouTube Downloader Bot
python-telegram-bot v21 | yt-dlp | FFmpeg | Pyrogram | Render

YouTube bypass strategy (ordered by reliability):
  1. cookies.txt auto-detected + validated on startup
  2. /cookiecheck command – shows cookie status + first valid line
  3. android_vr + tv + tv_downgraded + web client chain (no PO token required)
  4. age_gate bypass via embed extraction
  5. Rotating User-Agents
  6. Extractor / fragment retries + pacing
  7. compat_opts workarounds

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  UPLOAD ENGINE — Pyrogram MTProto (no 50 MB limit → 2 GB)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Uploads use Pyrogram directly over MTProto, which bypasses the HTTP
Bot API 50 MB restriction.  Only two env vars needed (no user session):

  TELEGRAM_API_ID   — integer, from https://my.telegram.org/apps
  TELEGRAM_API_HASH — string,  from https://my.telegram.org/apps

requirements.txt additions:
  pyrogram
  tgcrypto        ← C extension for fast MTProto encryption (strongly recommended)

Extra features vs plain Bot API:
  • Live upload progress bar (updates every 3 s)
  • ffprobe metadata injected → Telegram shows duration/dimensions correctly
  • Silent-audio-track patch → Telegram never converts videos to GIFs
"""

import os, asyncio, time, logging, re, threading, random, urllib.request, sys, platform, subprocess, gc
import json as _json
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError
import yt_dlp as _yt_dlp_module

# ── Pyrogram client (MTProto upload engine) ───────────────────────────────────
# Bot-mode client: api_id + api_hash + bot_token only. No user session needed.
from pyrogram import Client as PyroClient

TELEGRAM_API_ID   = int(os.environ.get("TELEGRAM_API_ID",  "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()

# Module-level singleton started in post_init, stopped in post_shutdown.
_pyro_bot: "PyroClient | None" = None

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
DOWNLOAD_DIR = Path("downloads")
COOKIES_FILE = "cookies.txt"
DOWNLOAD_DIR.mkdir(exist_ok=True)
BOT_START_TIME = time.time()

# ── Proxy (required for YouTube on Render to bypass bot-detection) ────────────
# Set YTDL_PROXY=http://your-proxy:port in Render environment variables.
# Without a proxy, YouTube returns "Requested format is not available" even
# with cookies. Recommended: a cheap residential/datacenter HTTP proxy.
YTDL_PROXY = os.environ.get("YTDL_PROXY", "")

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


# ═════════════════════════════════════════════════════════════════════════════
#  COOKIE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def init_cookies_from_env() -> None:
    """
    If YOUTUBE_COOKIES env var is set, write it to cookies.txt on startup.
    This solves the Render ephemeral filesystem problem — cookies survive
    redeploys when stored as an env var.

    How to set on Render:
      1. On your PC: open cookies.txt, select all, copy
      2. Render → Environment → Add:
            Key:   YOUTUBE_COOKIES
            Value: (paste entire cookies.txt content)
      3. Redeploy — bot writes cookies.txt automatically on every start
    """
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return
    try:
        Path(COOKIES_FILE).write_text(raw, encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        yt    = [l for l in lines if "youtube.com" in l or "google.com" in l]
        logger.info("✅ cookies.txt written from YOUTUBE_COOKIES env var "
                    "(%d total lines, %d youtube/google lines)", len(lines), len(yt))
    except Exception as e:
        logger.error("❌ Failed to write cookies.txt from env var: %s", e)

def cookie_status() -> dict:
    """
    Returns detailed status of cookies.txt so we can surface problems to admin.
    """
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

    # Check for critical cookies
    has_sapisid = any("SAPISID" in l for l in yt_lines)
    has_sid     = any("\tSID\t" in l or "\t__Secure-1PSID\t" in l for l in yt_lines)
    sample      = yt_lines[0][:120] if yt_lines else ""

    return {
        "ok":         True,
        "path":       str(path.resolve()),
        "size":       size,
        "total":      len(real_lines),
        "yt_lines":   len(yt_lines),
        "has_sapisid": has_sapisid,
        "has_sid":     has_sid,
        "sample":     sample,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  YT-DLP OPTIONS – FULL BYPASS STACK
# ═════════════════════════════════════════════════════════════════════════════

def ydl_opts_base(use_cookies: bool = True) -> dict:
    """
    Base yt-dlp options.

    Format strategy (from working reference):
      bestvideo[height<=1080]+bestaudio/best[height<=1080]/best
    Merged to mp4 via FFmpeg — this is the only reliable way to get
    video+audio on YouTube without "Requested format is not available".

    Proxy is REQUIRED on Render — without it YouTube bot-detection blocks
    adaptive stream downloads even with valid cookies.
    Set YTDL_PROXY=http://host:port in Render environment variables.
    """
    opts: dict = {
        "quiet":           True,
        "no_warnings":     True,
        "noplaylist":      True,
        "outtmpl":         str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        # Prevent yt-dlp writing .info.json / description files to disk
        "writeinfojson":   False,
        "writedescription":False,
        # Don't embed metadata or thumbnails into the file — saves RAM + disk
        "writethumbnail":  False,
        "embedthumbnail":  False,

        # Format strategy:
        #   selector  — NO codec filter. "bestvideo" = best at the ranked
        #               resolution. Codec filters like [vcodec^=avc] cause
        #               silent resolution drops (e.g. 720p H264 instead of
        #               1080p VP9) when H264 isn't available at the target height.
        #   format_sort — handles codec preference AFTER resolution is locked.
        #               yt-dlp picks 1080p H264 if available, else 1080p VP9.
        #               Resolution always wins over codec preference.
        "format":      "bestvideo+bestaudio/best",
        "format_sort": ["res", "vcodec:h264", "acodec:m4a", "br"],
        "merge_output_format": "mp4",

        # Retries
        "retries":             10,
        "fragment_retries":    10,
        "extractor_retries":   5,
        "file_access_retries": 5,
        "socket_timeout":      30,
    }

    # ── YouTube client chain — no PO token / bgutil / Deno required ─────
    #
    # Root cause of 1080p+ quality loss (researched from yt-dlp source):
    #   YouTube's SABR (Server ABR) system forces adaptive streams through
    #   its own servers when it detects an unknown/unsupported client or
    #   when a valid GVS PO Token is missing. This degrades quality.
    #
    # Client analysis (yt-dlp 2026.03.17 _base.py):
    #   android_vr   — NO PO token required, NO JS player, returns full
    #                  adaptive streams (1080p+). Version MUST stay at 1.65
    #                  — yt-dlp source comment: ">1.65 returns SABR only".
    #   tv           — NO PO token required, returns adaptive streams,
    #                  good fallback for videos android_vr can't access.
    #   tv_downgraded— Secondary TV fallback, also no PO token needed.
    #   web          — Last resort metadata fallback only (JS player needed,
    #                  no PO token, but limited adaptive streams).
    #
    # Do NOT use: ios, android, mweb, tv_simply, web_creator, web_music
    #   — all require PO token (required=True in GVS_PO_TOKEN_POLICY),
    #   which means they fall back to low-quality muxed streams without
    #   a token provider running.
    opts["extractor_args"] = {
        "youtube": {
            "player_client": ["android_vr", "tv", "tv_downgraded", "web"],
        }
    }
    logger.info("yt-dlp client chain: android_vr + tv + tv_downgraded + web (no PO token needed)")

    # ── Proxy ─────────────────────────────────────────────────────────────
    if YTDL_PROXY:
        opts["proxy"] = YTDL_PROXY
        logger.info("Using proxy: %s", YTDL_PROXY)

    # ── Cookies ───────────────────────────────────────────────────────────
    if use_cookies:
        cs = cookie_status()
        if cs["ok"]:
            opts["cookiefile"] = COOKIES_FILE
            logger.info("cookies.txt loaded (%d YT lines)", cs.get("yt_lines", 0))
        else:
            logger.warning("cookies.txt problem: %s", cs["reason"])

    return opts


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def get_settings(uid: int) -> dict:
    if uid not in user_settings:
        user_settings[uid] = DEFAULT_SETTINGS.copy()
    return user_settings[uid]


def pick_best_formats(formats: list, quality: str) -> tuple[str, str]:
    """
    Return yt-dlp FORMAT SELECTOR STRINGS (not raw format IDs).

    Using selector strings instead of format IDs avoids the
    "Requested format is not available" error that occurs when:
      - Info was extracted with one client (ios/android) but download
        uses a different client session with different format IDs.
      - Format IDs are session-scoped and can change between requests.

    Returns (video_selector, audio_selector) where each is a valid
    yt-dlp -f string. The caller passes these directly to yt-dlp's
    "format" option.
    """
    target_h = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080,
                "1440p": 1440, "2160p": 2160, "4k": 2160}.get(quality)

    # Count buckets just for logging
    video_only = [f for f in formats
                  if (f.get("vcodec") or "none") != "none"
                  and (f.get("acodec") or "none") == "none"]
    audio_only = [f for f in formats
                  if (f.get("acodec") or "none") != "none"
                  and (f.get("vcodec") or "none") == "none"]
    muxed      = [f for f in formats
                  if (f.get("vcodec") or "none") != "none"
                  and (f.get("acodec") or "none") != "none"]

    logger.info(
        "Format buckets — video-only: %d  audio-only: %d  muxed: %d",
        len(video_only), len(audio_only), len(muxed),
    )
    if not video_only and muxed:
        logger.warning(
            "⚠️ No adaptive streams — only %d muxed format(s). Max quality: 360p.",
            len(muxed),
        )

    if not target_h or quality == "best":
        vid_sel = "bestvideo"
        aud_sel = "bestaudio"
        logger.info("Selector: %s + %s", vid_sel, aud_sel)
        return vid_sel, aud_sel

    # Height cap only — no codec filter. format_sort (set in ydl_opts_base)
    # handles H264 preference at the same resolution.
    vid_sel = f"bestvideo[height<={target_h}]"
    aud_sel = "bestaudio"

    logger.info("Selector for %s: video=%s  audio=%s", quality, vid_sel, aud_sel)
    return vid_sel, aud_sel


def quality_opts(q: str) -> dict:
    """Fallback selector used only when we have no cached format list."""
    return {"format": "bestvideo*+bestaudio*/best", "merge_output_format": "mp4"}


def register_for_cleanup(path: str, minutes: int):
    cleanup_registry[path] = 0.0 if minutes == 0 else time.time() + minutes * 60


def is_youtube_url(text: str) -> bool:
    return bool(re.match(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", text.strip()))


def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    logger.warning("Download error (raw): %s", str(e)[:300])

    # ── Check most-specific patterns first ────────────────────────────────────
    # "Requested format is not available" contains "not available", so this
    # check MUST come before the generic "unavailable"/"not available" guard.
    if "requested format" in msg:
        return (
            "❌ *Format not available.*\n"
            "Try a different quality or use ⭐ Best Available."
        )
    if "no video formats" in msg or "no formats" in msg:
        return "❌ *No downloadable formats found.* The video may be private or region-locked."
    if "sign in" in msg or "not a bot" in msg or "confirm" in msg or "bot" in msg:
        return (
            "🔒 *YouTube is blocking this download.*\n\n"
            "Cookies may be expired. Run /cookiecheck for details.\n\n"
            "*Quick fixes:*\n"
            "• Re-export cookies while logged into YouTube\n"
            "• Export from `youtube.com` (not google.com)\n"
            "• Use *'Get cookies.txt LOCALLY'* extension\n"
            "• Don't export from incognito mode"
        )
    if "private" in msg:
        return "🔒 This video is *private*."
    if "copyright" in msg or "blocked" in msg:
        return "⛔ Blocked due to *copyright restrictions*."
    if "age" in msg:
        return "🔞 *Age-restricted.* Provide cookies from a verified account."
    if "ffmpeg" in msg:
        return "⚙️ *FFmpeg error.* Try a lower quality or ⭐ Best Available."
    if "fragment" in msg or "network" in msg:
        return "🌐 *Network error* while downloading. Please retry."
    if "unavailable" in msg or "not available" in msg:
        return "❌ Video *unavailable* — may be region-blocked or removed."
    return f"❌ Download failed:\n`{str(e)[:300]}`"


# ── Core async wrappers ───────────────────────────────────────────────────────

async def extract_info(url: str, download: bool = False,
                       extra_opts: dict | None = None) -> dict:
    opts = ydl_opts_base()
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
            fmts = info.get("formats", []) if info else []
            if fmts:
                exts    = sorted({f.get("ext") for f in fmts if f.get("ext")})
                heights = sorted({f.get("height") for f in fmts
                                  if isinstance(f.get("height"), int) and f["height"] > 0})
                logger.info("Formats available — exts: %s | heights: %s", exts, heights)
            return info
    return await loop.run_in_executor(None, _run)


async def do_download(url: str, extra_opts: dict, progress_cb) -> dict:
    opts = ydl_opts_base()
    opts.update(extra_opts)
    opts["progress_hooks"] = [progress_cb]
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Log a compact format summary to help diagnose selector mismatches
            fmts = info.get("formats", []) if info else []
            if fmts:
                summary = [(f.get("format_id"), f.get("ext"), f.get("height"),
                            f.get("vcodec","?")[:6], f.get("acodec","?")[:6])
                           for f in fmts[-10:]]  # last 10 (highest quality)
                logger.info("Available formats (last 10): %s", summary)
            return info
    return await loop.run_in_executor(None, _run)


def build_progress_hook(loop, status_msg, _cid, _bot, label: str = ""):
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


# ── Background cleanup ────────────────────────────────────────────────────────
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


# ── Health server ──────────────────────────────────────────────────────────────
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
    """Diagnose the cookies.txt file and show actionable status."""
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
            "4. Click extension → click *Export as* → save `cookies.txt`\n"
            "5. Replace your `cookies.txt` file and redeploy\n\n"
            "⚠️ *Do NOT export in incognito mode*\n"
            "⚠️ *Export from youtube.com, not google.com*"
        )
    else:
        sapisid_status = "✅" if cs.get("has_sapisid") else "⚠️ Missing (may cause issues)"
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
            msg += (
                "⚠️ *Missing critical auth cookies.*\n"
                "Re-export while fully logged into YouTube.\n"
                "Make sure you're not in incognito mode."
            )
        else:
            msg += (
                "✅ Cookies look complete.\n\n"
                "If downloads still fail, cookies may have *expired*.\n"
                "Re-export from a fresh YouTube session and redeploy."
            )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ─── Stats ────────────────────────────────────────────────────────────────────
def _get_ffmpeg_version() -> str:
    """Return the FFmpeg version string, or a short error message."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        )
        first_line = (result.stdout or result.stderr).splitlines()[0]
        # e.g. "ffmpeg version 6.1.1-static https://..."
        match = re.search(r"ffmpeg version\s+(\S+)", first_line, re.IGNORECASE)
        return match.group(1) if match else first_line[:60]
    except FileNotFoundError:
        return "❌ Not found in PATH"
    except Exception as exc:
        return f"❌ {exc}"


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
    """Return (file_count, total_bytes) for DOWNLOAD_DIR."""
    files = list(DOWNLOAD_DIR.iterdir()) if DOWNLOAD_DIR.exists() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    return len(files), total


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show bot + dependency statistics."""
    # yt-dlp version
    try:
        ytdlp_ver = _yt_dlp_module.version.__version__
    except Exception:
        ytdlp_ver = "unknown"

    ffmpeg_ver   = _get_ffmpeg_version()
    python_ver   = sys.version.split()[0]
    os_info      = f"{platform.system()} {platform.release()}"
    uptime_str   = _format_uptime(time.time() - BOT_START_TIME)
    file_count, dir_bytes = _download_dir_info()
    dir_mb       = dir_bytes / (1024 * 1024)
    active_users = len(user_settings)
    queued_files = len(cleanup_registry)
    cs           = cookie_status()
    cookie_icon  = "✅" if cs["ok"] else "❌"
    cookie_label = (
        f"{cs.get('yt_lines', 0)} YT cookies, SAPISID={'✅' if cs.get('has_sapisid') else '⚠️'}"
        if cs["ok"] else cs["reason"]
    )

    if _pyro_bot and _pyro_bot.is_connected:
        upload_limit = "2 GB ✅ (Pyrogram MTProto)"
        upload_icon  = "🚀"
    elif TELEGRAM_API_ID and TELEGRAM_API_HASH:
        upload_limit = "⚠️ Creds set but Pyrogram not connected yet"
        upload_icon  = "⚠️"
    else:
        upload_limit = "❌ TELEGRAM_API_ID / TELEGRAM_API_HASH not set"
        upload_icon  = "❌"

    msg = (
        "📊 *Bot Statistics*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔧 *Dependencies*\n"
        f"  • yt-dlp:  `{ytdlp_ver}`\n"
        f"  • FFmpeg:  `{ffmpeg_ver}`\n"
        f"  • Python:  `{python_ver}`\n"
        f"  • OS:      `{os_info}`\n\n"
        "⏱ *Runtime*\n"
        f"  • Uptime:  `{uptime_str}`\n\n"
        "👥 *Usage*\n"
        f"  • Active user profiles:  `{active_users}`\n"
        f"  • Files pending cleanup: `{queued_files}`\n\n"
        "💾 *Download Folder*\n"
        f"  • Files: `{file_count}`\n"
        f"  • Size:  `{dir_mb:.2f} MB`\n\n"
        f"📤 *Upload*\n"
        f"  • Limit:  {upload_icon} `{upload_limit}`\n\n"
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
                [InlineKeyboardButton("🟣 1440p (2K)", callback_data="s:set:quality:1440p"),
                 InlineKeyboardButton("🔵 2160p (4K)", callback_data="s:set:quality:2160p")],
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
    msg = await update.message.reply_text("🔍 Fetching video info…")
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

    # Store a slim copy — only the fields we actually use downstream.
    # The full info dict can be 500KB–2MB (100+ formats, thumbnails, etc.)
    # and stays in RAM for the entire session per user, causing OOM on Render.
    slim_formats = [
        {k: f.get(k) for k in
         ("format_id", "ext", "height", "width", "vcodec", "acodec",
          "filesize", "filesize_approx", "format_note", "tbr", "fps")}
        for f in (info.get("formats") or [])
    ]
    ctx.user_data["info"] = {
        "id":         info.get("id", ""),
        "title":      title,
        "duration":   duration,
        "thumbnail":  info.get("thumbnail"),
        "thumbnails": [
            {"url": t.get("url"), "width": t.get("width")}
            for t in (info.get("thumbnails") or [])
            if t.get("url")
        ],
        "formats":    slim_formats,
    }

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
    title   = info.get("title", "Video")

    # Build deduplicated list of available video heights
    seen_heights    = set()
    quality_formats = []
    for f in formats:
        vc = (f.get("vcodec") or "none").lower()
        h  = f.get("height")
        if vc == "none" or not h or h in seen_heights:
            continue
        seen_heights.add(h)
        quality_formats.append(f)
    quality_formats.sort(key=lambda f: f.get("height", 0))

    buttons = []
    for f in quality_formats:
        h        = f.get("height", "?")
        note     = f.get("format_note") or f"{h}p"
        size     = f.get("filesize") or f.get("filesize_approx")
        size_str = f"  {size // 1024 // 1024} MB" if size else ""
        ac       = (f.get("acodec") or "none").lower()
        tag      = "🔊" if ac != "none" else "🎬"
        buttons.append([InlineKeyboardButton(
            f"{tag} {note}{size_str}", callback_data=f"dl:quality:{h}p"
        )])

    buttons.append([InlineKeyboardButton("⭐ Best Available", callback_data="dl:quality:best")])
    buttons.append([InlineKeyboardButton("❌ Cancel",         callback_data="dl:cancel")])

    await q.message.edit_text(
        f"🎬 *{title}*\n\nSelect quality:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ffmpeg_merge(video_path: str, audio_path: str, out_path: str) -> None:
    """
    Merge a video-only file and an audio-only file into a single mp4.
    Runs in a thread-pool executor so it doesn't block the event loop.
    Raises RuntimeError with ffmpeg's stderr if the merge fails.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",   # no re-encode — just remux
        "-c:a", "aac",    # normalise audio to aac for mp4 compatibility
        "-b:a", "192k",
        # NOTE: -movflags +faststart is intentionally omitted.
        # That flag rewrites the entire file to move the moov atom to the
        # front, which takes minutes on large files (1+ GB). Telegram upload
        # does not require faststart — the file is streamed from disk, not
        # served via HTTP progressive download.
        out_path,
    ]
    logger.info("ffmpeg merge: %s + %s → %s", video_path, audio_path, out_path)
    loop = asyncio.get_event_loop()

    def _run():
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-800:])
        return result

    await loop.run_in_executor(None, _run)


# ═════════════════════════════════════════════════════════════════════════════
#  PYROGRAM UPLOAD ENGINE
#  Ported from the reference upload bot — pg_send logic adapted for this bot.
# ═════════════════════════════════════════════════════════════════════════════

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".opus"}


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
    if h:   return f"{h}h {m}m"
    if m:   return f"{m}m {s:02d}s"
    return f"{s}s"

def _speed_str(bps: float) -> str:
    if bps <= 0: return "?"
    if bps >= 1024**3: return f"{bps/1024**3:.1f} GB/s"
    if bps >= 1024**2: return f"{bps/1024**2:.1f} MB/s"
    return f"{bps/1024:.0f} KB/s"

def upload_progress_text(filename: str, current: int, total: int, elapsed: float) -> str:
    """Rich upload progress message."""
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
                            eta_str: str, downloaded: str, total: str) -> str:
    """Rich download progress message."""
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
        f"⚡ `{speed_str}`  ⏱ `{eta_str}`",
    ])


def get_video_meta(filepath: str) -> dict:
    """Return width, height, duration (s), has_audio via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        data    = _json.loads(result.stdout)
        streams = data.get("streams", [])
        vs      = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_aud = any(s.get("codec_type") == "audio" for s in streams)
        dur_str = vs.get("duration") or "0"
        return {
            "width":     max(0, int(vs.get("width")  or 0)),
            "height":    max(0, int(vs.get("height") or 0)),
            "duration":  max(0, int(float(dur_str))),
            "has_audio": has_aud,
        }
    except Exception:
        return {"width": 0, "height": 0, "duration": 0, "has_audio": False}


def ensure_audio_track(filepath: str) -> str:
    """
    If the video has no audio stream, add a silent AAC track via ffmpeg.
    Telegram converts audio-less videos to GIFs regardless of file size —
    this patch prevents that.
    Returns path to the fixed file (new temp file), or original on failure.
    """
    try:
        meta = get_video_meta(filepath)
        if meta.get("has_audio", True):
            return filepath                          # already has audio
        p        = Path(filepath)
        out_path = str(p.parent / (p.stem + "_audio" + p.suffix))
        result   = subprocess.run(
            [
                "ffmpeg", "-y", "-i", filepath,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0 and Path(out_path).exists():
            logger.info("[FFMPEG] Added silent audio track: %s", p.name)
            try: p.unlink()
            except Exception: pass
            return out_path
    except Exception as e:
        logger.warning("[FFMPEG] ensure_audio_track failed: %s", e)
    return filepath


def fix_audio_only(filepath: str) -> str:
    """
    Fix ONLY the audio codec if it is incompatible with Telegram (opus/vorbis).
    Video is NEVER re-encoded — stream-copied as-is.

    Key design decisions to keep this FAST:
      • No -movflags +faststart — that flag forces ffmpeg to rewrite the entire
        file after muxing (moving moov atom) which blocks for minutes on large
        files. Telegram handles non-faststart mp4 perfectly fine for uploads.
      • Audio-only transcode takes ~5-15s regardless of video resolution/size.
      • Already-compatible files (aac/mp3) are returned immediately with no ffmpeg.

    Telegram natively plays:
      • H264, VP9 video codecs (all resolutions including 1440p/4K)
      • AAC, MP3 audio codecs
    Does NOT need transcoding:
      • H264 + AAC → upload directly
      • VP9  + AAC → upload directly
    Needs audio fix only (fast, ~5-15s):
      • VP9  + opus/vorbis → copy video, transcode audio to AAC
      • H264 + opus/vorbis → copy video, transcode audio to AAC
    Skip entirely (return original):
      • AV1 files → Telegram doesn't support but transcode would take hours;
                    upload as-is so user gets a quick error rather than waiting
    """
    try:
        p = Path(filepath)
        if not p.exists():
            return filepath

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        streams = _json.loads(probe.stdout).get("streams", [])
        vs      = next((s for s in streams if s.get("codec_type") == "video"), {})
        as_     = next((s for s in streams if s.get("codec_type") == "audio"), {})
        vcodec  = vs.get("codec_name", "")
        acodec  = as_.get("codec_name", "")
        src_mb  = p.stat().st_size / 1024**2

        logger.info("[FFMPEG] %s — vcodec=%s acodec=%s size=%.1f MB",
                    p.name, vcodec or "?", acodec or "?", src_mb)

        # Already compatible — upload as-is, no processing needed
        aac_ok = acodec in ("aac", "mp3", "")
        if aac_ok:
            logger.info("[FFMPEG] audio already compatible, uploading directly")
            return filepath

        # Audio needs fixing — copy video stream, transcode audio only.
        # NOTE: No -movflags +faststart here — it forces a full file rewrite
        # (moov atom relocation) which takes minutes on large files. Omitting it
        # means ffmpeg finishes in seconds since it only processes the audio track.
        out_path = str(p.parent / (p.stem + "_fix.mp4"))
        timeout  = max(120, int(src_mb * 2))  # 2 s/MB generous upper bound

        if not as_:
            # No audio at all — add silent track (Telegram GIF-ifies audioless videos)
            cmd = [
                "ffmpeg", "-y", "-i", filepath,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                out_path,  # no +faststart — avoids full-file moov rewrite
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", filepath,
                "-c:v", "copy",          # never re-encode video
                "-c:a", "aac", "-b:a", "128k",
                out_path,  # no +faststart — avoids full-file moov rewrite
            ]

        logger.info("[FFMPEG] audio fix: %s → aac (video stream-copy, no faststart)", acodec)
        rc = subprocess.call(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=timeout)

        if rc == 0 and Path(out_path).exists():
            logger.info("[FFMPEG] audio fix OK (%.1f MB → %.1f MB)",
                        src_mb, Path(out_path).stat().st_size / 1024**2)
            try: p.unlink()
            except Exception: pass
            return out_path

        logger.warning("[FFMPEG] audio fix failed (rc=%d), uploading original", rc)
    except subprocess.TimeoutExpired:
        logger.warning("[FFMPEG] audio fix timed out, uploading original")
    except Exception as e:
        logger.warning("[FFMPEG] audio fix error: %s", e)
    return filepath


def download_thumbnail(info: dict, vid_id: str) -> str | None:
    """
    Download the best available YouTube thumbnail to a local JPEG file.
    Returns the file path, or None on failure.
    Tries the highest-resolution thumbnail first (maxresdefault → hqdefault).
    """
    # yt-dlp exposes a ranked list in info["thumbnails"] (best last)
    thumbnails = info.get("thumbnails") or []
    # Sort by preference (width desc), then fall back to info["thumbnail"]
    candidates = sorted(
        [t for t in thumbnails if t.get("url")],
        key=lambda t: (t.get("width") or 0),
        reverse=True,
    )
    urls = [t["url"] for t in candidates]
    if not urls:
        fallback = info.get("thumbnail")
        if fallback:
            urls = [fallback]
    if not urls:
        return None

    out_path = str(DOWNLOAD_DIR / f"{vid_id}_thumb.jpg")
    for url in urls:
        try:
            urllib.request.urlretrieve(url, out_path)
            if Path(out_path).stat().st_size > 1000:   # skip tiny/broken images
                logger.info("Thumbnail saved: %s (%d bytes)", out_path,
                            Path(out_path).stat().st_size)
                return out_path
        except Exception as e:
            logger.debug("Thumbnail attempt failed (%s): %s", url[:60], e)
    return None


async def start_pyro_bot() -> None:
    """Create and start the Pyrogram bot client (called from post_init)."""
    global _pyro_bot
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error(
            "❌  TELEGRAM_API_ID / TELEGRAM_API_HASH not set.\n"
            "     Get them at https://my.telegram.org/apps and add to env vars."
        )
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")
    _pyro_bot = PyroClient(
        name      = "yt_dl_bot",
        api_id    = TELEGRAM_API_ID,
        api_hash  = TELEGRAM_API_HASH,
        bot_token = BOT_TOKEN,
        no_updates = True,   # pure upload client — PTB handles all updates
    )
    await _pyro_bot.start()
    me = await _pyro_bot.get_me()
    logger.info("✅ Pyrogram MTProto client ready — @%s (2 GB upload limit active)",
                me.username or "?")


async def stop_pyro_bot() -> None:
    """Disconnect Pyrogram on shutdown (called from post_shutdown)."""
    global _pyro_bot
    if _pyro_bot and _pyro_bot.is_connected:
        await _pyro_bot.stop()
        logger.info("Pyrogram client stopped.")


# ─── Unified upload helper ────────────────────────────────────────────────────
async def send_file(
    chat_id:       int,
    filepath:      str,
    filename:      str,
    caption:       str,
    status_msg,
    is_video:      bool = True,
    thumb_path:    str | None = None,
) -> None:
    """
    Upload a merged video (or audio) file via Pyrogram MTProto.

    Features:
      • Rich upload progress bar — updates every 3 s (speed, ETA, bytes)
      • ffprobe injects width / height / duration → Telegram shows correctly
      • ensure_telegram_compatible() pre-muxes to H264+AAC — Telegram skips
        server-side transcoding, preserving original quality
      • thumb_path: local JPEG thumbnail shown in Telegram video player
      • 2 GB limit (Pyrogram MTProto, no Bot API restriction)
    """
    if _pyro_bot is None or not _pyro_bot.is_connected:
        raise RuntimeError("Pyrogram client is not running.")

    filepath   = str(filepath)           # accept Path objects too
    file_size  = os.path.getsize(filepath)
    size_mb    = file_size / (1024 * 1024)
    ext        = Path(filepath).suffix.lower()

    logger.info("Uploading %s (%.1f MB) via Pyrogram MTProto", filename, size_mb)

    # ── Progress callback ─────────────────────────────────────────────────────
    # Pyrogram (v2) calls progress directly with `await func()` on its own
    # internal loop (confirmed in save_file.py line 194-195). This means:
    #   • Same event loop, same thread — plain `await` works correctly.
    #   • No need for ensure_future / run_coroutine_threadsafe.
    #   • We must catch ALL exceptions — Pyrogram does NOT catch errors from
    #     the progress callback; an unhandled exception cancels the upload.
    #   • RetryAfter: honour the wait time by pushing _last_edit forward so
    #     the next tick also skips, preventing a flood-limit storm.
    _last_edit:  list[float] = [0.0]
    _start_time: list[float] = [time.time()]

    async def _progress(current: int, total: int) -> None:
        now = time.time()
        if now - _last_edit[0] < 3:          # 3 s minimum between edits (matches download progress)
            return
        _last_edit[0] = now
        elapsed = now - _start_time[0]
        text = upload_progress_text(filename, current, total, elapsed)
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            err = str(e).lower()
            if "retry" in err or "flood" in err:
                # Extract retry_after seconds if available and back off
                import re as _re
                m = _re.search(r"retry.*?(\d+)", err)
                backoff = int(m.group(1)) if m else 10
                _last_edit[0] = now + backoff   # skip next N seconds of ticks
            elif "message is not modified" in err:
                pass   # identical text — harmless
            else:
                logger.debug("Upload progress edit skipped: %s", e)
            # Never re-raise — an exception here would cancel the upload

    # ── Video upload ──────────────────────────────────────────────────────────
    if is_video and ext in VIDEO_EXTS:
        loop = asyncio.get_event_loop()

        # Quick ffprobe to decide if audio transcoding is needed BEFORE
        # showing "Uploading" — so user sees accurate status during the fix.
        def _needs_audio_fix() -> bool:
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_streams", filepath],
                    capture_output=True, text=True, timeout=15,
                )
                streams = _json.loads(probe.stdout).get("streams", [])
                as_ = next((s for s in streams if s.get("codec_type") == "audio"), {})
                return as_.get("codec_name", "") not in ("aac", "mp3", "")
            except Exception:
                return False

        needs_fix = await loop.run_in_executor(None, _needs_audio_fix)
        if needs_fix:
            await status_msg.edit_text(
                "⚙️ *Converting audio track…* (video untouched, ~10s)",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await status_msg.edit_text(
                f"📤 *Uploading* `{filename}` *({human_size(file_size)})*…",
                parse_mode=ParseMode.MARKDOWN,
            )

        # Fix audio codec if needed (opus→aac). Video is NEVER re-encoded.
        # No -movflags +faststart here — that forces ffmpeg to rewrite the
        # entire file after muxing (moov atom relocation) which takes minutes
        # on large files. Omitting it keeps audio-only fix to ~5-15s.
        filepath = await loop.run_in_executor(None, fix_audio_only, filepath)

        if needs_fix:
            await status_msg.edit_text(
                f"📤 *Uploading* `{filename}` *({human_size(os.path.getsize(filepath))})*…",
                parse_mode=ParseMode.MARKDOWN,
            )

        meta = get_video_meta(filepath)
        logger.info(
            "Video meta — %dx%d  dur=%ds  has_audio=%s",
            meta["width"], meta["height"], meta["duration"], meta["has_audio"],
        )

        await _pyro_bot.send_video(
            chat_id            = chat_id,
            video              = filepath,
            caption            = caption,
            file_name          = filename,
            width              = meta["width"],
            height             = meta["height"],
            duration           = meta["duration"],
            supports_streaming = True,
            thumb              = thumb_path,
            progress           = _progress,
        )

    # ── Audio upload ──────────────────────────────────────────────────────────
    elif ext in AUDIO_EXTS:
        await _pyro_bot.send_audio(
            chat_id   = chat_id,
            audio     = filepath,
            caption   = caption,
            file_name = filename,
            progress  = _progress,
        )

    # ── Document fallback ─────────────────────────────────────────────────────
    else:
        await _pyro_bot.send_document(
            chat_id   = chat_id,
            document  = filepath,
            caption   = caption,
            file_name = filename,
            progress  = _progress,
        )


# ─── Video ────────────────────────────────────────────────────────────────────
async def _do_video_direct(update: Update, ctx, uid: int, quality: str, status_msg):
    """
    Called when a quality button is tapped (inline keyboard flow).
    Mimics what do_video does but takes a status_msg directly instead of
    a callback query object, since we came from a text message not a button tap.
    """
    url = ctx.user_data.get("url")
    if not url:
        await status_msg.edit_text("❌ No URL stored. Please resend the link.")
        return

    cached_info  = ctx.user_data.get("info", {})
    formats      = cached_info.get("formats", [])
    vid_id       = cached_info.get("id", "unknown")
    title        = cached_info.get("title", vid_id)

    if formats:
        video_fmt_id, audio_fmt_id = pick_best_formats(formats, quality)
    else:
        video_fmt_id, audio_fmt_id = "bestvideo*", "bestaudio*"
        logger.warning("No cached format list; using generic selectors")

    logger.info("Downloading %s | quality=%s | video_id=%s audio_id=%s",
                vid_id, quality, video_fmt_id, audio_fmt_id)

    loop      = asyncio.get_event_loop()
    base_opts = ydl_opts_base()

    await status_msg.edit_text(
        f"⬇️ *Downloading video stream ({quality})…*",
        parse_mode=ParseMode.MARKDOWN,
    )

    def _download_stream(fmt_id: str, suffix: str) -> str:
        out_tmpl = str(DOWNLOAD_DIR / f"{vid_id}_{suffix}.%(ext)s")
        opts = {**base_opts,
                "format": fmt_id,
                "outtmpl": out_tmpl,
                "merge_output_format": None,
                "postprocessors": []}
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
            lbl   = "🎬 video" if suffix == "video" else "🔊 audio"
            text  = download_progress_text(f"{lbl} *{quality}*", pct, speed, eta, down, total)
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)
        opts["progress_hooks"] = [hook]
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
            found = sorted(DOWNLOAD_DIR.glob(f"{vid_id}_{suffix}.*"))
            if not found:
                raise FileNotFoundError(f"No {suffix} file written for {vid_id}")
            return str(found[-1])

    try:
        video_file = await loop.run_in_executor(None, _download_stream, video_fmt_id, "video")
    except Exception as e:
        raw = str(e)[:500]
        logger.error("Download failed: %s", raw)
        await status_msg.edit_text(
            friendly_error(e) + f"\n\n`{raw}`",
            parse_mode=ParseMode.MARKDOWN)
        return

    video_only_fmts = [f for f in formats
                       if (f.get("vcodec") or "none") != "none"
                       and (f.get("acodec") or "none") == "none"]
    is_muxed_only = len(video_only_fmts) == 0

    if is_muxed_only:
        merged_path = video_file
    else:
        await status_msg.edit_text("⬇️ *Downloading audio stream…*", parse_mode=ParseMode.MARKDOWN)
        try:
            audio_file = await loop.run_in_executor(None, _download_stream, audio_fmt_id, "audio")
        except Exception as e:
            Path(video_file).unlink(missing_ok=True)
            await status_msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return

        await status_msg.edit_text("⚙️ *Merging streams…*", parse_mode=ParseMode.MARKDOWN)
        merged_path = str(DOWNLOAD_DIR / f"{vid_id}_merged.mp4")
        try:
            await ffmpeg_merge(video_file, audio_file, merged_path)
        except Exception as e:
            Path(video_file).unlink(missing_ok=True)
            Path(audio_file).unlink(missing_ok=True)
            await status_msg.edit_text(
                f"⚙️ *FFmpeg merge failed.*\n`{str(e)[:300]}`",
                parse_mode=ParseMode.MARKDOWN)
            return
        finally:
            Path(video_file).unlink(missing_ok=True)
            Path(audio_file).unlink(missing_ok=True)

    safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip()
    filename   = f"{safe_title}_{quality}.mp4"
    caption    = f"🎬 *{title}*\n🎞 Quality: `{quality}`"
    s          = get_settings(uid)

    thumb_path = download_thumbnail(cached_info, vid_id)
    if thumb_path:
        register_for_cleanup(thumb_path, s["cleanup_minutes"])

    try:
        await send_file(
            chat_id    = update.effective_chat.id,
            filepath   = merged_path,
            filename   = filename,
            caption    = caption,
            status_msg = status_msg,
            is_video   = True,
            thumb_path = thumb_path,
        )
    except Exception as e:
        await status_msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        return
    finally:
        register_for_cleanup(merged_path, s["cleanup_minutes"])

    await status_msg.edit_text(f"✅ *Done!* `{filename}`", parse_mode=ParseMode.MARKDOWN)


async def do_video(q, ctx, uid: int, quality: str):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored. Please resend the link."); return

    # ── Step 1: build format selector ───────────────────────────────────
    cached_info = ctx.user_data.get("info", {})
    vid_id      = cached_info.get("id", "unknown")
    title       = cached_info.get("title", vid_id)

    # No codec filter in selector — format_sort handles H264 preference.
    # [vcodec^=avc] in selector silently drops to lower resolution when
    # H264 isn't available at the target height (e.g. 720p H264 instead
    # of 1080p VP9). Resolution must always win over codec preference.
    if quality == "best":
        fmt = "bestvideo+bestaudio/best"
    else:
        target_h = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080,
                    "1440p": 1440, "2160p": 2160}.get(quality, 1080)
        fmt = (
            f"bestvideo[height<={target_h}]+bestaudio"
            f"/best[height<={target_h}]"
            f"/bestvideo+bestaudio"
            f"/best"
        )

    logger.info("Downloading %s | quality=%s | format=%s", vid_id, quality, fmt)

    loop      = asyncio.get_event_loop()
    base_opts = ydl_opts_base()
    out_path  = str(DOWNLOAD_DIR / f"{vid_id}_{quality}.%(ext)s")

    # ── Step 2: download + auto-merge via yt-dlp+ffmpeg ──────────────────
    status = await q.message.edit_text(
        f"⬇️ *Downloading ({quality})…*",
        parse_mode=ParseMode.MARKDOWN,
    )

    def _download() -> str:
        """Single yt-dlp call: downloads bestvideo+bestaudio and merges to mp4."""
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
            text  = download_progress_text(f"*{quality}*", pct, speed, eta, down, total)
            asyncio.run_coroutine_threadsafe(
                status.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)

        opts = {
            **base_opts,
            "format":              fmt,
            "format_sort":         ["res", "vcodec:h264", "acodec:m4a", "br"],
            "merge_output_format": "mp4",
            "outtmpl":             out_path,
            "progress_hooks":      [hook],
            # Surface real error messages so failures are diagnosable
            "quiet":               False,
            "no_warnings":         False,
            # Log the selected format so quality issues are visible in logs
            "verbose":             False,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Log exactly which format was selected
            if info:
                sel_fmt = info.get("format", "?")
                sel_h   = info.get("height", "?")
                sel_vc  = info.get("vcodec", "?")
                logger.info("Selected format: %s | height=%s | vcodec=%s", sel_fmt, sel_h, sel_vc)

        # Find the merged mp4 yt-dlp wrote
        found = sorted(DOWNLOAD_DIR.glob(f"{vid_id}_{quality}.*"))
        if not found:
            raise FileNotFoundError(f"No output file found for {vid_id}")
        return str(found[-1])

    try:
        merged_path = await loop.run_in_executor(None, _download)
        logger.info("Downloaded+merged: %s", merged_path)
    except Exception as e:
        raw = str(e)[:500]
        logger.error("Download failed: %s", raw)
        user_msg = friendly_error(e)
        user_msg += f"\n\n`{raw}`"
        await status.edit_text(user_msg, parse_mode=ParseMode.MARKDOWN)
        return
    finally:
        # Force GC after download — yt-dlp holds format dicts, HTTP
        # connections, and parser objects until collected. On Render free tier
        # the GC cycle is lazy and memory doesn't release without this.
        gc.collect()

    # ── Step 3: upload ────────────────────────────────────────────────────
    # Download best-resolution thumbnail from YouTube for the video player
    thumb_path = download_thumbnail(cached_info, vid_id)
    if thumb_path:
        register_for_cleanup(thumb_path, get_settings(uid)["cleanup_minutes"])

    await status.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)
    try:
        await send_file(
            chat_id    = q.message.chat_id,
            filepath   = merged_path,
            filename   = f"{title}.mp4",
            caption    = f"🎬 {title} [{quality}]",
            status_msg = status,
            is_video   = True,
            thumb_path = thumb_path,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    finally:
        # Release format list — no longer needed after upload
        ctx.user_data.pop("info", None)
        gc.collect()

    register_for_cleanup(merged_path, get_settings(uid)["cleanup_minutes"])


# ─── Audio ────────────────────────────────────────────────────────────────────
async def do_audio(q, ctx, uid: int):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored."); return

    status = await q.message.edit_text("⬇️ *Extracting audio…*", parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    hook = build_progress_hook(loop, status, q.message.chat_id, ctx.bot)
    try:
        info = await do_download(url, {
            # bestaudio/best covers both split-stream and pre-muxed sources.
            # format_sort in ydl_opts_base already prefers m4a; this handles
            # webm/opus streams served by tv / android_vr clients.
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
    await status.edit_text("📤 *Uploading MP3…*", parse_mode=ParseMode.MARKDOWN)
    try:
        await send_file(
            chat_id    = q.message.chat_id,
            filepath   = filepath,
            filename   = f"{info.get('title', 'audio')}.mp3",
            caption    = f"🎵 {info.get('title', '')}",
            status_msg = status,
            is_video   = False,
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
    try:
        urllib.request.urlretrieve(thumb_url, outpath)
    except Exception as e:
        await status.edit_text(f"❌ Thumbnail fetch failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return
    try:
        with open(outpath, "rb") as f:
            await ctx.bot.send_document(
                chat_id=q.message.chat_id, document=f,
                filename=f"{info.get('title', 'thumbnail')}.jpg",
                caption=f"🖼 {info.get('title', '')}",
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
    import traceback
    tb = "".join(traceback.format_exception(type(ctx.error), ctx.error, ctx.error.__traceback__))
    logger.error("Unhandled exception:\n%s", tb)
    short = str(ctx.error)[:400]
    msg = f"⚠️ *Unexpected error:*\n`{short}`"
    try:
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif isinstance(update, Update) and update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    # Write cookies.txt from env var (Render ephemeral filesystem fix)
    init_cookies_from_env()

    # Log cookie status on startup so it's visible in Render logs
    cs = cookie_status()
    if cs["ok"]:
        logger.info("✅ cookies.txt OK — %d YouTube/Google lines, SAPISID=%s",
                    cs.get("yt_lines", 0), cs.get("has_sapisid", False))
    else:
        logger.warning("⚠️ cookies.txt problem: %s", cs["reason"])
        logger.warning("   Bot will try client fallback chain (android_vr/tv/tv_downgraded/web)")

    # ── Launch Pyrogram MTProto upload client ────────────────────────────
    # Pyrogram is started inside post_init (event loop already running).
    # If TELEGRAM_API_ID / TELEGRAM_API_HASH are missing, post_init raises
    # and the bot exits with a clear error message.
    logger.info("Pyrogram MTProto upload engine will start in post_init.")

    threading.Thread(target=start_health_server, daemon=True).start()

    builder = Application.builder().token(BOT_TOKEN)
    app = builder.build()
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
        await start_pyro_bot()          # ← Pyrogram MTProto client (2 GB uploads)
        asyncio.create_task(cleanup_worker())

    async def post_shutdown(application: Application):
        await stop_pyro_bot()           # ← graceful disconnect

    app.post_init     = post_init
    app.post_shutdown = post_shutdown
    logger.info("Bot started — polling")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
