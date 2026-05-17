"""
Advanced Telegram YouTube Downloader Bot
python-telegram-bot v21 | yt-dlp | FFmpeg | Render

YouTube bypass strategy (ordered by reliability):
  1. cookies.txt auto-detected + validated on startup
  2. /cookiecheck command – shows cookie status + first valid line
  3. tv_embedded + mweb + android_music + ios client chain
  4. age_gate bypass via embed extraction
  5. Rotating User-Agents
  6. Extractor / fragment retries + pacing
  7. compat_opts workarounds
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
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError
import yt_dlp as _yt_dlp_module

# ── Local Bot API Server (2 GB uploads on Render) ────────────────────────────
# Run telegram-bot-api locally on Render as a separate service.
# Set LOCAL_API_URL=http://<your-render-service>:8081 to enable 2 GB uploads.
# If not set, falls back to official api.telegram.org (50 MB limit).
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "").rstrip("/")

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

# 50 MB = official Bot API limit. Local server removes this cap entirely.
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024

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
    Layered bypass:
      • cookies.txt  (when valid)
      • tv_embedded  → bypasses age-gate & most sign-in checks
      • mweb         → mobile web, lighter bot-detection
      • android_music → no sign-in enforcement
      • ios          → tertiary
      • web          → standard web (last resort)

    format_sort ensures yt-dlp prefers mp4/m4a so the format selectors
    in quality_opts() actually match what gets served.
    """
    opts: dict = {
        "quiet":       True,
        "no_warnings": True,
        "noplaylist":  True,
        "outtmpl":     str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),

        # Always merge to mp4 so the output is universally playable
        "merge_output_format": "mp4",

        # Retries
        "retries":             10,
        "fragment_retries":    10,
        "extractor_retries":   5,
        "file_access_retries": 5,
        "socket_timeout":      30,

        # Human-like pacing
        "sleep_interval_requests": 1,
        "sleep_interval":          2,
        "max_sleep_interval":      5,

        # Browser impersonation
        "http_headers": {
            "User-Agent":      random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "DNT":             "1",
            "Sec-Fetch-Mode":  "navigate",
        },

        # ── Client fallback chain ─────────────────────────────────────────
        # tv_embedded  → YouTube TV embed, bypasses sign-in/age-gate entirely
        # mweb         → YouTube mobile web (lighter detection)
        # android_music → YouTube Music Android app (no sign-in wall)
        # ios          → YouTube iOS app
        # web          → standard web (last resort)
        #
        # NOTE: Do NOT set player_skip here. Skipping "webpage" or "configs"
        # prevents yt-dlp from fetching the initial player response that
        # contains the format manifest — the result is an empty format list
        # and every format selector throws "Requested format is not available".
        "extractor_args": {
            "youtube": {
                # ios + android → return full adaptive format list (360p–1080p+)
                # tv_embedded   → age-gate bypass (only returns muxed 360p — fallback only)
                # android_music → last resort
                "player_client": ["ios", "android", "tv_embedded", "android_music"],
            }
        },

        # Compatibility workarounds for age-gated / restricted content
        "compat_opts": {"no-youtube-unavailable-videos"},
    }

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
        # Best available: yt-dlp picks the highest quality adaptive pair
        vid_sel = "bestvideo[ext=mp4]/bestvideo"
        aud_sel = "bestaudio[ext=m4a]/bestaudio"
        logger.info("Selector: %s + %s", vid_sel, aud_sel)
        return vid_sel, aud_sel

    # Height-based selector: at or below target, best available below that
    # The [height<=N] filter is evaluated fresh at download time against the
    # actual available formats — no stale format ID problem.
    vid_sel = (
        f"bestvideo[height<={target_h}][ext=mp4]"
        f"/bestvideo[height<={target_h}]"
        f"/bestvideo[ext=mp4]"
        f"/bestvideo"
    )
    aud_sel = "bestaudio[ext=m4a]/bestaudio"

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


def build_progress_hook(loop, status_msg, _cid, _bot):
    last = [0.0]
    def hook(d):
        if d["status"] != "downloading": return
        now = time.time()
        if now - last[0] < 3: return
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

    if LOCAL_API_URL:
        upload_limit = f"2 GB (local server: {LOCAL_API_URL})"
        upload_icon  = "🚀"
    else:
        upload_limit = "50 MB (set LOCAL_API_URL for 2 GB)"
        upload_icon  = "⚠️"

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

    # ── Quality selection by number reply (like reference bot logic) ──────
    if ctx.user_data.get("awaiting_quality"):
        await handle_quality_reply(update, ctx, text)
        return

    if is_youtube_url(text):
        await handle_youtube_url(update, ctx, text)
    else:
        await handle_search(update, ctx, text)


async def handle_quality_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle the user's numbered quality reply, matching reference bot logic."""
    uid             = update.effective_user.id
    quality_formats = ctx.user_data.get("quality_formats", [])
    best_idx        = len(quality_formats) + 1   # last option = Best Available

    try:
        selection = int(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please reply with a number from the quality list."
        )
        return

    if selection < 1 or selection > best_idx:
        await update.message.reply_text(
            f"⚠️ Please enter a number between 1 and {best_idx}."
        )
        return

    # Clear awaiting flag immediately so stray messages don't re-trigger
    ctx.user_data["awaiting_quality"] = False
    ctx.user_data.pop("quality_formats", None)

    if selection == best_idx:
        quality = "best"
    else:
        chosen_fmt = quality_formats[selection - 1]
        h = chosen_fmt.get("height")
        quality = f"{h}p" if h else "best"

    # Simulate a callback-query-like call to do_video by building a thin shim
    class _MsgShim:
        """Minimal shim so do_video can call status.edit_text."""
        def __init__(self, msg):
            self._msg = msg
            self._bot = msg._bot if hasattr(msg, "_bot") else None
        async def edit_text(self, *a, **kw):
            # edit_text doesn't exist on a fresh reply — use reply_text instead
            return await self._msg.reply_text(*a, **kw)

    status_msg = await update.message.reply_text(
        f"⏳ *Starting download ({quality})…*", parse_mode=ParseMode.MARKDOWN
    )

    # Patch status_msg so do_video's edit_text calls work
    original_reply = status_msg.reply_text
    async def _edit_text(*a, **kw):
        return await status_msg.edit_text(*a, **kw)
    status_msg.edit_text = _edit_text   # already has edit_text, this is fine

    await _do_video_direct(update, ctx, uid, quality, status_msg)


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

    # Build a deduplicated list of ALL downloadable formats:
    # adaptive video-only streams + muxed streams (like reference code logic
    # but without the acodec!=none filter that hides high-res adaptive streams)
    seen_heights = set()
    quality_formats = []

    for f in formats:
        vc = (f.get("vcodec") or "none").lower()
        ac = (f.get("acodec") or "none").lower()
        has_v = vc != "none"
        h = f.get("height")
        if not has_v or not h:
            continue
        if h in seen_heights:
            continue
        seen_heights.add(h)
        quality_formats.append(f)

    # Sort by height ascending
    quality_formats.sort(key=lambda f: f.get("height", 0))

    # Add "Best Available" at the end
    # Store the format list in user_data so number reply can resolve it
    ctx.user_data["quality_formats"] = quality_formats
    ctx.user_data["awaiting_quality"] = True

    lines = []
    for idx, f in enumerate(quality_formats):
        h        = f.get("height", "?")
        note     = f.get("format_note") or f"{h}p"
        size     = f.get("filesize") or f.get("filesize_approx")
        size_str = f"{size // 1024 // 1024} MB" if size else "? MB"
        ac       = (f.get("acodec") or "none").lower()
        tag      = "🔊" if ac != "none" else "🎬"  # muxed vs video-only
        lines.append(f"{idx + 1}. {tag} {note} — {size_str}")

    lines.append(f"{len(quality_formats) + 1}. ⭐ Best Available")

    quality_list = "\n".join(lines)
    title = info.get("title", "Video")

    await q.message.edit_text(
        f"🎬 *{title}*\n\n"
        f"Choose a quality by replying with the number:\n\n"
        f"{quality_list}",
        parse_mode=ParseMode.MARKDOWN,
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
        "-movflags", "+faststart",  # web-optimised atom order
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


# ─── Unified upload helper ────────────────────────────────────────────────────
async def send_file(
    chat_id: int,
    filepath: str,
    filename: str,
    caption: str,
    status_msg,
    is_video: bool = True,
) -> None:
    """
    Upload a file to Telegram.

    • LOCAL_API_URL set → uses local Bot API server (up to 2 GB, HTTP only,
      works perfectly on Render — no MTProto TCP needed).
    • LOCAL_API_URL not set → official api.telegram.org (50 MB hard limit).

    To enable 2 GB uploads on Render:
      1. Deploy telegram-bot-api as a separate Render service (Docker image:
         aiogram/telegram-bot-api or ghcr.io/tdlib/telegram-bot-api).
      2. Set LOCAL_API_URL=http://<service-name>:8081 in this bot's env vars.
      3. Also set TG_API_ID and TG_API_HASH (from https://my.telegram.org/apps)
         in the telegram-bot-api service env vars.
    """
    file_size = os.path.getsize(filepath)
    size_mb   = file_size / (1024 * 1024)
    via_local = bool(LOCAL_API_URL)

    logger.info("Uploading %s (%.1f MB) via %s",
                filename, size_mb, f"local API ({LOCAL_API_URL})" if via_local else "official Bot API")

    if not via_local and file_size > LARGE_FILE_THRESHOLD:
        raise RuntimeError(
            f"File is {size_mb:.0f} MB which exceeds the 50 MB Bot API limit.\n\n"
            "To upload files up to 2 GB on Render, set up a local Bot API server:\n"
            "1. Deploy telegram-bot-api on Render (Docker: aiogram/telegram-bot-api)\n"
            "2. Set LOCAL_API_URL=http://<service>:8081 in this bot's env vars\n"
            "3. Set TG_API_ID + TG_API_HASH in the telegram-bot-api service"
        )

    await status_msg.edit_text(
        f"📤 *Uploading ({size_mb:.1f} MB)…*" +
        (" via local server" if via_local else ""),
        parse_mode=ParseMode.MARKDOWN,
    )

    with open(filepath, "rb") as fh:
        if is_video and filepath.endswith(".mp4"):
            await status_msg._bot.send_video(
                chat_id=chat_id,
                video=fh,
                caption=caption,
                filename=filename,
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=30,
            )
        else:
            await status_msg._bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename=filename,
                caption=caption,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=30,
            )


# ─── Video ────────────────────────────────────────────────────────────────────
async def _do_video_direct(update: Update, ctx, uid: int, quality: str, status_msg):
    """
    Called from handle_quality_reply (number-reply flow).
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
            pct   = d.get("_percent_str", "?%").strip()
            speed = d.get("_speed_str",   "?").strip()
            eta   = d.get("_eta_str",     "?").strip()
            label = "🎬 video" if suffix == "video" else "🔊 audio"
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(
                    f"⬇️ *Downloading {label} stream ({quality})…*\n"
                    f"`{pct}` at `{speed}` — ETA `{eta}`",
                    parse_mode=ParseMode.MARKDOWN,
                ), loop)
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
        await status_msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
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

    try:
        await send_file(
            chat_id    = update.effective_chat.id,
            filepath   = merged_path,
            filename   = filename,
            caption    = caption,
            status_msg = status_msg,
            is_video   = True,
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

    # ── Step 1: pick format IDs from cached info ──────────────────────────
    cached_info = ctx.user_data.get("info", {})
    formats     = cached_info.get("formats", [])
    vid_id      = cached_info.get("id", "unknown")
    title       = cached_info.get("title", vid_id)

    if formats:
        video_fmt_id, audio_fmt_id = pick_best_formats(formats, quality)
    else:
        # No cached info — fall back to generic selector
        video_fmt_id, audio_fmt_id = "bestvideo*", "bestaudio*"
        logger.warning("No cached format list; using generic selectors")

    logger.info("Downloading %s | quality=%s | video_id=%s audio_id=%s",
                vid_id, quality, video_fmt_id, audio_fmt_id)

    loop = asyncio.get_event_loop()
    base_opts = ydl_opts_base()

    # ── Step 2: download video-only stream ───────────────────────────────
    status = await q.message.edit_text(
        f"⬇️ *Downloading video stream ({quality})…*",
        parse_mode=ParseMode.MARKDOWN,
    )
    video_path_raw: list[str] = []

    def _download_stream(fmt_id: str, suffix: str) -> str:
        """Download a single stream and return the path of the saved file."""
        out_tmpl = str(DOWNLOAD_DIR / f"{vid_id}_{suffix}.%(ext)s")
        opts = {**base_opts,
                "format": fmt_id,
                "outtmpl": out_tmpl,
                "merge_output_format": None,  # no auto-merge — we do it ourselves
                "postprocessors": [],}
        last = [0.0]
        def hook(d):
            if d["status"] != "downloading": return
            now = time.time()
            if now - last[0] < 3: return
            last[0] = now
            pct   = d.get("_percent_str",  "?%").strip()
            speed = d.get("_speed_str",    "?").strip()
            eta   = d.get("_eta_str",      "?").strip()
            label = "🎬 video" if suffix == "video" else "🔊 audio"
            asyncio.run_coroutine_threadsafe(
                status.edit_text(
                    f"⬇️ *Downloading {label} stream ({quality})…*\n"
                    f"`{pct}` at `{speed}` — ETA `{eta}`",
                    parse_mode=ParseMode.MARKDOWN,
                ),
                loop,
            )
        opts["progress_hooks"] = [hook]
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # yt-dlp may change the extension — find what it actually wrote
            found = sorted(DOWNLOAD_DIR.glob(f"{vid_id}_{suffix}.*"))
            if not found:
                raise FileNotFoundError(f"No {suffix} file written for {vid_id}")
            return str(found[-1])   # take the most recently written match

    try:
        video_file = await loop.run_in_executor(None, _download_stream, video_fmt_id, "video")
        logger.info("Video stream saved: %s", video_file)
    except Exception as e:
        await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        return

    # ── Step 3: download audio-only stream ───────────────────────────────
    # Skip if video stream already has audio (muxed format like format 18)
    video_only_formats = [f for f in formats
                          if (f.get("vcodec") or "none") != "none"
                          and (f.get("acodec") or "none") == "none"]
    is_muxed_only = len(video_only_formats) == 0

    if is_muxed_only:
        logger.info("Muxed-only stream — skipping separate audio download and merge")
        merged_path = video_file  # already has audio, use directly
    else:
        await status.edit_text("⬇️ *Downloading audio stream…*", parse_mode=ParseMode.MARKDOWN)
        try:
            audio_file = await loop.run_in_executor(None, _download_stream, audio_fmt_id, "audio")
            logger.info("Audio stream saved: %s", audio_file)
        except Exception as e:
            Path(video_file).unlink(missing_ok=True)
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return

        # ── Step 4: ffmpeg merge ──────────────────────────────────────────────
        await status.edit_text("⚙️ *Merging streams…*", parse_mode=ParseMode.MARKDOWN)
        merged_path = str(DOWNLOAD_DIR / f"{vid_id}_merged.mp4")
        try:
            await ffmpeg_merge(video_file, audio_file, merged_path)
            logger.info("Merged: %s", merged_path)
        except Exception as e:
            logger.error("ffmpeg merge failed: %s", e)
            Path(video_file).unlink(missing_ok=True)
            Path(audio_file).unlink(missing_ok=True)
            await status.edit_text(
                f"⚙️ *FFmpeg merge failed.*\n`{str(e)[:300]}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        finally:
            # Temp streams no longer needed regardless of merge outcome
            Path(video_file).unlink(missing_ok=True)
            Path(audio_file).unlink(missing_ok=True)

    # ── Step 5: upload ────────────────────────────────────────────────────
    await status.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)
    try:
        await send_file(
            chat_id  = q.message.chat_id,
            filepath = merged_path,
            filename = f"{title}.mp4",
            caption  = f"🎬 {title} [{quality}]",
            status_msg = status,
            is_video = True,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

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
            # webm/opus streams served by tv_embedded / android_music clients.
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
    logger.error("Unhandled exception:", exc_info=ctx.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("⚠️ Unexpected error. Please try again.")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    # Log cookie status on startup so it's visible in Render logs
    cs = cookie_status()
    if cs["ok"]:
        logger.info("✅ cookies.txt OK — %d YouTube/Google lines, SAPISID=%s",
                    cs.get("yt_lines", 0), cs.get("has_sapisid", False))
    else:
        logger.warning("⚠️ cookies.txt problem: %s", cs["reason"])
        logger.warning("   Bot will try client fallback chain (tv_embedded/android_music/ios)")

    # ── Upload limit check ────────────────────────────────────────────────
    if LOCAL_API_URL:
        logger.info("✅ Local Bot API server: %s (limit: 2 GB per file)", LOCAL_API_URL)
    else:
        logger.warning("⚠️ LOCAL_API_URL not set — uploads capped at 50 MB (official Bot API)")

    threading.Thread(target=start_health_server, daemon=True).start()

    # Point python-telegram-bot at the local server when configured.
    # The local server accepts the same HTTP API but with no file-size cap.
    builder = Application.builder().token(BOT_TOKEN)
    if LOCAL_API_URL:
        builder = builder.base_url(f"{LOCAL_API_URL}/bot")
        builder = builder.base_file_url(f"{LOCAL_API_URL}/file/bot")
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
        asyncio.create_task(cleanup_worker())

    async def post_shutdown(application: Application):
        pass

    app.post_init     = post_init
    app.post_shutdown = post_shutdown
    logger.info("Bot started — polling")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # discard stale updates from previous session
    )


if __name__ == "__main__":
    main()
