"""
Advanced Telegram YouTube Downloader Bot
Uses: python-telegram-bot v20+, yt-dlp, FFmpeg
Hosted on Render with health check server
"""

import os
import asyncio
import json
import time
import logging
import re
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DOWNLOAD_DIR: Path = Path("downloads")
COOKIES_FILE: str = "cookies.txt"          # Netscape cookies file (optional)
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ─── Default user settings ───────────────────────────────────────────────────
DEFAULT_SETTINGS: dict = {
    "quality": "720p",          # 360p | 480p | 720p | 1080p | best
    "mode": "manual",           # fixed | manual
    "cleanup_minutes": 10,      # 5 | 10 | 15 | 30 | 0 (never)
}

# In-memory stores  (swap for Redis / SQLite for multi-instance)
user_settings: dict[int, dict] = {}
# { file_path: expire_timestamp }  0 = never
cleanup_registry: dict[str, float] = {}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_settings(user_id: int) -> dict:
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
    return user_settings[user_id]


def ydl_opts_base() -> dict:
    """Base yt-dlp options shared across all calls."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    }
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def quality_to_format(quality: str) -> str:
    """Convert quality string to yt-dlp format selector."""
    mapping = {
        "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]",
        "480p":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
        "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]",
        "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    }
    return mapping.get(quality, mapping["720p"])


def register_for_cleanup(path: str, minutes: int):
    """Add a file to the cleanup registry."""
    if minutes == 0:
        cleanup_registry[path] = 0.0   # never
    else:
        cleanup_registry[path] = time.time() + minutes * 60


def is_youtube_url(text: str) -> bool:
    pattern = r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+"
    return bool(re.match(pattern, text.strip()))


async def extract_info(url: str, download: bool = False, extra_opts: dict | None = None) -> dict:
    """Run yt-dlp info extraction in executor (non-blocking)."""
    opts = ydl_opts_base()
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_event_loop()

    def _extract():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)

    return await loop.run_in_executor(None, _extract)


async def do_download(url: str, extra_opts: dict, progress_cb) -> dict:
    """Download with progress hook, returns info dict."""
    opts = ydl_opts_base()
    opts.update(extra_opts)
    opts["progress_hooks"] = [progress_cb]
    loop = asyncio.get_event_loop()

    def _dl():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    return await loop.run_in_executor(None, _dl)


def build_progress_hook(loop, status_msg, chat_id, bot):
    """Returns a synchronous progress hook that schedules async edits."""
    last_edit = [0.0]

    def hook(d):
        if d["status"] == "downloading":
            now = time.time()
            if now - last_edit[0] < 3:      # throttle to every 3 s
                return
            last_edit[0] = now
            pct   = d.get("_percent_str", "?").strip()
            speed = d.get("_speed_str", "?").strip()
            eta   = d.get("_eta_str", "?").strip()
            text  = f"⬇️ *Downloading…*\n`{pct}` | Speed: `{speed}` | ETA: `{eta}`"
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN),
                loop,
            )
    return hook


# ─── Background cleanup task ──────────────────────────────────────────────────

async def cleanup_worker():
    """Runs forever, deletes expired files every 60 s."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [
            p for p, t in list(cleanup_registry.items())
            if t != 0.0 and t < now
        ]
        for path in expired:
            try:
                Path(path).unlink(missing_ok=True)
                del cleanup_registry[path]
                logger.info("Cleaned up: %s", path)
            except Exception as e:
                logger.warning("Cleanup error for %s: %s", path, e)


# ─── Health-check HTTP server (required for Render) ───────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass   # suppress access logs


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health server listening on port %d", port)


# ─── /start ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to YT Downloader Bot!*\n\n"
        "Send me:\n"
        "• A *YouTube URL* to download video / audio / thumbnail\n"
        "• A *song name* to search YouTube\n\n"
        "Commands:\n"
        "/settings – Manage your preferences\n"
        "/help – Show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ─── /help ───────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ─── /settings ───────────────────────────────────────────────────────────────

def settings_keyboard(uid: int) -> InlineKeyboardMarkup:
    s = get_settings(uid)
    quality_label = s["quality"].upper()
    mode_label    = "Fixed ✅" if s["mode"] == "fixed" else "Manual 🎛"
    timer_val     = s["cleanup_minutes"]
    timer_label   = "♾ Never" if timer_val == 0 else f"{timer_val} min"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Default Quality: {quality_label}", callback_data="s:quality")],
        [InlineKeyboardButton(f"🔁 Download Mode: {mode_label}",      callback_data="s:mode")],
        [InlineKeyboardButton(f"🧹 Cleanup Timer: {timer_label}",     callback_data="s:cleanup")],
        [InlineKeyboardButton("❌ Close",                              callback_data="s:close")],
    ])


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "⚙️ *Your Settings*\nTap an option to change it:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(uid),
    )


# ─── Settings callback handler ────────────────────────────────────────────────

async def settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    data = q.data   # e.g. "s:quality", "s:set:quality:720p"

    parts = data.split(":")

    # ── Top-level menu navigation ────────────────────────────────────────────
    if parts[1] == "close":
        await q.message.delete()
        return

    if parts[1] == "back":
        await q.message.edit_text(
            "⚙️ *Your Settings*\nTap an option to change it:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(uid),
        )
        return

    # ── Sub-menus ────────────────────────────────────────────────────────────
    if parts[1] == "quality" and len(parts) == 2:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("360p",  callback_data="s:set:quality:360p"),
             InlineKeyboardButton("480p",  callback_data="s:set:quality:480p")],
            [InlineKeyboardButton("720p",  callback_data="s:set:quality:720p"),
             InlineKeyboardButton("1080p", callback_data="s:set:quality:1080p")],
            [InlineKeyboardButton("⭐ Best Available", callback_data="s:set:quality:best")],
            [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
        ])
        await q.message.edit_text(
            "🎬 *Select Default Video Quality:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    if parts[1] == "mode" and len(parts) == 2:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Fixed Quality",    callback_data="s:set:mode:fixed")],
            [InlineKeyboardButton("🎛 Manual Selection", callback_data="s:set:mode:manual")],
            [InlineKeyboardButton("⬅️ Back",             callback_data="s:back")],
        ])
        await q.message.edit_text(
            "🔁 *Select Download Mode:*\n\n"
            "• *Fixed* – always use your default quality\n"
            "• *Manual* – choose quality per download",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    if parts[1] == "cleanup" and len(parts) == 2:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("5 min",  callback_data="s:set:cleanup:5"),
             InlineKeyboardButton("10 min", callback_data="s:set:cleanup:10")],
            [InlineKeyboardButton("15 min", callback_data="s:set:cleanup:15"),
             InlineKeyboardButton("30 min", callback_data="s:set:cleanup:30")],
            [InlineKeyboardButton("♾ Never", callback_data="s:set:cleanup:0")],
            [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
        ])
        await q.message.edit_text(
            "🧹 *Auto-Cleanup Timer:*\nFiles are deleted after this duration.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    # ── Apply setting ─────────────────────────────────────────────────────────
    if parts[1] == "set" and len(parts) == 4:
        _, _, key, value = parts
        s = get_settings(uid)
        if key == "quality":
            s["quality"] = value
        elif key == "mode":
            s["mode"] = value
        elif key == "cleanup":
            s["cleanup_minutes"] = int(value)
        await q.message.edit_text(
            f"✅ *Setting saved!*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(uid),
        )
        return


# ─── Main message handler ─────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = update.effective_user.id

    if is_youtube_url(text):
        await handle_youtube_url(update, ctx, text)
    else:
        await handle_search(update, ctx, text)


# ─── YouTube URL entry point ──────────────────────────────────────────────────

async def handle_youtube_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    uid = update.effective_user.id
    msg = await update.message.reply_text("🔍 Fetching video info…")

    try:
        info = await extract_info(url)
    except (DownloadError, ExtractorError) as e:
        await msg.edit_text(f"❌ *Could not fetch video:*\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        await msg.edit_text(f"❌ Unexpected error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    title    = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    dur_str  = f"{duration//60}m {duration%60}s" if duration else "?"

    # Store URL in context for callback
    ctx.user_data["url"]  = url
    ctx.user_data["info"] = info

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Video",      callback_data="dl:video")],
        [InlineKeyboardButton("🎵 Audio MP3",  callback_data="dl:audio")],
        [InlineKeyboardButton("🖼 Thumbnail",  callback_data="dl:thumb")],
        [InlineKeyboardButton("❌ Cancel",     callback_data="dl:cancel")],
    ])
    await msg.edit_text(
        f"📹 *{title}*\n⏱ Duration: `{dur_str}`\n\nChoose download type:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


# ─── Download callback dispatcher ─────────────────────────────────────────────

async def download_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    data = q.data   # dl:video | dl:audio | dl:thumb | dl:cancel | dl:quality:<q> | dl:search:<idx>

    parts = data.split(":")

    if parts[1] == "cancel":
        await q.message.edit_text("❌ Download cancelled.")
        return

    if parts[1] == "thumb":
        await do_thumbnail(q, ctx, uid)
        return

    if parts[1] == "audio":
        await do_audio(q, ctx, uid)
        return

    if parts[1] == "video":
        s = get_settings(uid)
        if s["mode"] == "fixed":
            await do_video(q, ctx, uid, s["quality"])
        else:
            await show_quality_menu(q, ctx, uid)
        return

    if parts[1] == "quality" and len(parts) == 3:
        await do_video(q, ctx, uid, parts[2])
        return

    if parts[1] == "search" and len(parts) == 3:
        # User picked a search result
        idx  = int(parts[2])
        results = ctx.user_data.get("search_results", [])
        if idx < len(results):
            url = results[idx].get("webpage_url") or results[idx].get("url")
            ctx.user_data["url"]  = url
            ctx.user_data["info"] = results[idx]
            await q.message.edit_text(
                f"🔗 Selected: *{results[idx].get('title','?')}*\nChoose download type:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Video",     callback_data="dl:video")],
                    [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
                    [InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")],
                    [InlineKeyboardButton("❌ Cancel",    callback_data="dl:cancel")],
                ]),
            )


# ─── Quality selection menu (manual mode) ────────────────────────────────────

async def show_quality_menu(q, ctx, uid: int):
    info = ctx.user_data.get("info", {})
    formats = info.get("formats", [])

    # Extract unique resolutions
    heights = sorted(set(
        f["height"] for f in formats
        if f.get("height") and f.get("vcodec") != "none"
    ))

    if not heights:
        await q.message.edit_text("⚠️ No format info found. Using best available.")
        await do_video(q, ctx, uid, "best")
        return

    buttons = []
    row = []
    for h in heights:
        label = f"{h}p"
        row.append(InlineKeyboardButton(label, callback_data=f"dl:quality:{label}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⭐ Best", callback_data="dl:quality:best")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])

    await q.message.edit_text(
        "🎬 *Select video quality:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─── Video download ───────────────────────────────────────────────────────────

async def do_video(q, ctx, uid: int, quality: str):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL found. Please send the link again.")
        return

    fmt = quality_to_format(quality)
    status_msg = await q.message.edit_text(f"⬇️ *Starting download ({quality})…*", parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    hook = build_progress_hook(loop, status_msg, q.message.chat_id, ctx.bot)

    extra = {
        "format": fmt,
        "merge_output_format": "mp4",
        "postprocessors": [],
    }

    try:
        info = await do_download(url, extra, hook)
    except DownloadError as e:
        await status_msg.edit_text(f"❌ *Download failed:*\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ Unexpected error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    # Find the downloaded file
    video_id = info.get("id", "")
    files = list(DOWNLOAD_DIR.glob(f"{video_id}.*"))
    if not files:
        await status_msg.edit_text("❌ Downloaded file not found on disk.")
        return

    filepath = str(files[0])
    await status_msg.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)

    try:
        with open(filepath, "rb") as f:
            await ctx.bot.send_document(
                chat_id=q.message.chat_id,
                document=f,
                filename=Path(filepath).name,
                caption=f"🎬 {info.get('title', '')} | {quality}",
            )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    s = get_settings(uid)
    register_for_cleanup(filepath, s["cleanup_minutes"])


# ─── Audio download ───────────────────────────────────────────────────────────

async def do_audio(q, ctx, uid: int):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL found.")
        return

    status_msg = await q.message.edit_text("⬇️ *Extracting audio…*", parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    hook = build_progress_hook(loop, status_msg, q.message.chat_id, ctx.bot)

    extra = {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    }

    try:
        info = await do_download(url, extra, hook)
    except DownloadError as e:
        await status_msg.edit_text(f"❌ *Audio extraction failed:*\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ Unexpected error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    video_id = info.get("id", "")
    files = list(DOWNLOAD_DIR.glob(f"{video_id}.mp3"))
    if not files:
        # yt-dlp sometimes names differently
        files = list(DOWNLOAD_DIR.glob(f"{video_id}.*"))
    if not files:
        await status_msg.edit_text("❌ Audio file not found on disk.")
        return

    filepath = str(files[0])
    await status_msg.edit_text("📤 *Uploading MP3…*", parse_mode=ParseMode.MARKDOWN)

    try:
        with open(filepath, "rb") as f:
            await ctx.bot.send_document(
                chat_id=q.message.chat_id,
                document=f,
                filename=f"{info.get('title','audio')}.mp3",
                caption=f"🎵 {info.get('title', '')}",
            )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    s = get_settings(uid)
    register_for_cleanup(filepath, s["cleanup_minutes"])


# ─── Thumbnail download ───────────────────────────────────────────────────────

async def do_thumbnail(q, ctx, uid: int):
    import aiohttp
    info = ctx.user_data.get("info", {})
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        await q.message.edit_text("❌ No thumbnail found for this video.")
        return

    status_msg = await q.message.edit_text("🖼 *Downloading thumbnail…*", parse_mode=ParseMode.MARKDOWN)
    vid_id  = info.get("id", "thumb")
    outpath = DOWNLOAD_DIR / f"{vid_id}_thumb.jpg"

    try:
        import urllib.request
        urllib.request.urlretrieve(thumb_url, outpath)
    except Exception as e:
        await status_msg.edit_text(f"❌ Thumbnail download failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        with open(outpath, "rb") as f:
            await ctx.bot.send_document(
                chat_id=q.message.chat_id,
                document=f,
                filename=f"{info.get('title','thumbnail')}.jpg",
                caption=f"🖼 {info.get('title', '')}",
            )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    s = get_settings(uid)
    register_for_cleanup(str(outpath), s["cleanup_minutes"])


# ─── Search ───────────────────────────────────────────────────────────────────

async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text(f"🔎 Searching YouTube for: *{query}*…", parse_mode=ParseMode.MARKDOWN)

    search_url = f"ytsearch5:{query}"
    try:
        results_info = await extract_info(
            search_url,
            download=False,
            extra_opts={"extract_flat": True},
        )
    except Exception as e:
        await msg.edit_text(f"❌ Search failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    entries = results_info.get("entries", [])
    if not entries:
        await msg.edit_text("😕 No results found.")
        return

    ctx.user_data["search_results"] = entries

    buttons = []
    for i, entry in enumerate(entries[:5]):
        title    = entry.get("title", "Unknown")[:50]
        duration = entry.get("duration", 0)
        dur_str  = f"{duration//60}:{duration%60:02d}" if duration else "?"
        buttons.append([
            InlineKeyboardButton(
                f"{i+1}. {title} [{dur_str}]",
                callback_data=f"dl:search:{i}",
            )
        ])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])

    await msg.edit_text(
        "🎵 *Top results:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─── Error handler ────────────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=ctx.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            "⚠️ An internal error occurred. Please try again later."
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_health_server()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))

    # Callback queries
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error handler
    app.add_error_handler(error_handler)

    # Set commands list
    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",    "Welcome message"),
            BotCommand("help",     "Help & usage"),
            BotCommand("settings", "Manage your preferences"),
        ])
        # Start cleanup worker
        asyncio.create_task(cleanup_worker())

    app.post_init = post_init

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
