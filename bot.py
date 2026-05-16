"""
Advanced Telegram YouTube Downloader Bot
python-telegram-bot v21 | yt-dlp | FFmpeg | Render
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

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

DEFAULT_SETTINGS = {"quality": "720p", "mode": "manual", "cleanup_minutes": 10}
user_settings:    dict[int, dict]  = {}
cleanup_registry: dict[str, float] = {}


# ═════════════════════════════════════════════════════════════════════════════
#  COOKIE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def cookie_status() -> dict:
    path = Path(COOKIES_FILE)
    if not path.exists():
        return {"ok": False, "reason": "File not found", "path": str(path.resolve())}
    size = path.stat().st_size
    if size < 100:
        return {"ok": False, "reason": f"File too small ({size} bytes)", "path": str(path.resolve()), "size": size}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as e:
        return {"ok": False, "reason": f"Cannot read file: {e}", "path": str(path.resolve())}

    real_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    yt_lines   = [l for l in real_lines if "youtube.com" in l or "google.com" in l]

    if not real_lines:
        return {"ok": False, "reason": "File has no cookie data", "path": str(path.resolve())}
    if not yt_lines:
        return {"ok": False, "reason": "No youtube.com or google.com cookies found", "path": str(path.resolve()), "total_lines": len(real_lines)}

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
    opts: dict = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "format_sort": ["res", "ext:mp4:m4a", "codec:h264:aac", "size"],
        "retries": 10, "fragment_retries": 10, "extractor_retries": 5,
        "file_access_retries": 5, "socket_timeout": 30,
        "sleep_interval_requests": 1, "sleep_interval": 2, "max_sleep_interval": 5,
        "http_headers": {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "DNT": "1",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "mweb", "android_music", "ios", "web"],
                "player_skip": ["webpage", "configs"],
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
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    h_up = h + 360
    return (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=webm]"
            f"/bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}][ext=mp4]/best[height<={h}]"
            f"/bestvideo[height<={h_up}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h_up}]+bestaudio"
            f"/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo+bestaudio/best")

def register_for_cleanup(path: str, minutes: int):
    cleanup_registry[path] = 0.0 if minutes == 0 else time.time() + minutes * 60

def is_youtube_url(text: str) -> bool:
    return bool(re.match(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", text.strip()))

def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "sign in" in msg or "cookie" in msg:
        return "🔒 *YouTube is blocking this video.*\nRun /cookiecheck to fix."
    if "private" in msg:
        return "🔒 This video is *private*."
    if "unavailable" in msg:
        return "❌ Video *unavailable*."
    if "age" in msg:
        return "🔞 *Age-restricted.* Provide cookies from a verified account."
    return f"❌ Download failed:\n`{str(e)[:400]}`"

async def extract_info(url: str, download: bool = False, extra_opts: dict | None = None) -> dict:
    opts = ydl_opts_base()
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)
    return await loop.run_in_executor(None, _run)

async def do_download(url: str, extra_opts: dict, progress_cb) -> dict:
    opts = ydl_opts_base()
    opts.update(extra_opts)
    opts["progress_hooks"] = [progress_cb]
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    return await loop.run_in_executor(None, _run)

def build_progress_hook(loop, status_msg, _cid, _bot):
    last = [0.0]
    def hook(d):
        if d["status"] != "downloading":
            return
        now = time.time()
        if now - last[0] < 3:
            return
        last[0] = now
        pct = d.get("_percent_str", "?%").strip()
        speed = d.get("_speed_str", "?").strip()
        eta = d.get("_eta_str", "?").strip()
        asyncio.run_coroutine_threadsafe(
            status_msg.edit_text(f"⬇️ *Downloading…*\n`{pct}` | 🚀 `{speed}` | ⏱ ETA `{eta}`",
                                 parse_mode=ParseMode.MARKDOWN), loop)
    return hook

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
        "Send me a YouTube URL or search query.\n"
        "⚙️ /settings – Preferences\n"
        "🍪 /cookiecheck – Diagnose cookies\n"
        "🔍 /formats <url> – Show all available formats (debug)",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_cookiecheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cs = cookie_status()
    if not cs["ok"]:
        msg = f"🍪 *Cookie Check — ❌ PROBLEM*\n📁 `{cs['path']}`\n❗ {cs['reason']}\n\n*Fix:* Re-export cookies from youtube.com while logged in."
    else:
        msg = (f"🍪 *Cookie Check — ✅ Valid*\n📁 `{cs['path']}`\n📦 {cs['size']} bytes\n"
               f"🎯 YouTube cookies: {cs['yt_lines']}\n🔑 SAPISID: {'✅' if cs['has_sapisid'] else '❌'}\n"
               f"🔑 SID: {'✅' if cs['has_sid'] else '❌'}")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_formats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Debug command: show all formats for a URL."""
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/formats <youtube_url>`", parse_mode=ParseMode.MARKDOWN)
        return
    url = args[0]
    status_msg = await update.message.reply_text("🔍 Fetching formats...")
    try:
        opts = ydl_opts_base()
        opts["extractor_args"]["youtube"]["player_client"] = ["web"]
        opts["extract_flat"] = False
        info = await extract_info(url, extra_opts=opts)
        formats = info.get("formats", [])
        lines = []
        for f in formats:
            height = f.get("height", "?")
            vcodec = f.get("vcodec", "none")
            if vcodec != "none" and height and height > 0:
                lines.append(f"{height}p")
        unique = sorted(set(lines), key=lambda x: int(x.replace("p","")))
        if unique:
            await status_msg.edit_text(f"📺 *Detected video heights:*\n`{', '.join(unique)}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await status_msg.edit_text("❌ No video formats found. Check cookies or video availability.")
    except Exception as e:
        await status_msg.edit_text(f"Error: `{e}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # ... (keep stats as before, but I'll include a simplified version)
    uptime = _format_uptime(time.time() - BOT_START_TIME)
    await update.message.reply_text(f"📊 *Stats*\nUptime: {uptime}\nUsers: {len(user_settings)}", parse_mode=ParseMode.MARKDOWN)

def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    mins, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins: parts.append(f"{mins}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

# ═════════════════════════════════════════════════════════════════════════════
#  QUALITY DISCOVERY (FIXED – TRIES MULTIPLE CLIENTS)
# ═════════════════════════════════════════════════════════════════════════════

async def get_all_heights(url: str) -> list[int]:
    """Try multiple clients to get all video heights."""
    clients = [["web"], ["android"], ["ios"], ["web", "android"]]
    for client_list in clients:
        try:
            opts = ydl_opts_base()
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": client_list,
                    "player_skip": ["webpage", "configs"],
                }
            }
            opts["extract_flat"] = False
            opts["quiet"] = True
            info = await extract_info(url, extra_opts=opts)
            heights = set()
            for f in info.get("formats", []):
                vcodec = f.get("vcodec", "")
                if vcodec == "none" or not vcodec:
                    continue
                h = f.get("height")
                if not h and f.get("resolution"):
                    res = f.get("resolution")
                    if "x" in res:
                        try:
                            h = int(res.split("x")[1])
                        except:
                            pass
                if h and isinstance(h, int) and h > 0:
                    heights.add(h)
            if heights:
                result = sorted(heights)
                logger.info(f"Client {client_list} found heights: {result}")
                return result
        except Exception as e:
            logger.warning(f"Client {client_list} failed: {e}")
            continue
    # Fallback
    logger.warning("No heights found, using fallback [360,480,720,1080]")
    return [360, 480, 720, 1080]

def get_thumbnail_safe(video_id: str, info: dict, out_dir: Path) -> str | None:
    thumb_url = info.get("thumbnail") or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    out_path = out_dir / f"{video_id}_thumb.jpg"
    try:
        urllib.request.urlretrieve(thumb_url, out_path, timeout=10)
        return str(out_path)
    except:
        try:
            fallback_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            urllib.request.urlretrieve(fallback_url, out_path, timeout=10)
            return str(out_path)
        except:
            return None


# ═════════════════════════════════════════════════════════════════════════════
#  HANDLERS
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
    except Exception as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        return
    title = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    dur_str = f"{duration // 60}m {duration % 60}s" if duration else "?"
    ctx.user_data["url"] = url
    ctx.user_data["info"] = info
    await msg.edit_text(
        f"📹 *{title}*\n⏱ `{dur_str}`\n\nWhat would you like?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Video", callback_data="dl:video")],
            [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")],
            [InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")],
        ]),
    )

async def download_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    parts = q.data.split(":")
    action = parts[1]

    if action == "cancel":
        await q.message.edit_text("❌ Cancelled.")
        return
    if action == "thumb":
        await do_thumbnail(q, ctx, uid)
        return
    if action == "audio":
        await do_audio(q, ctx, uid)
        return
    if action == "video":
        s = get_settings(uid)
        if s["mode"] == "fixed":
            await do_video(q, ctx, uid, s["quality"])
        else:
            await show_quality_menu(q, ctx)
        return
    if action == "quality" and len(parts) == 3:
        await do_video(q, ctx, uid, parts[2])
        return
    if action == "search" and len(parts) == 3:
        results = ctx.user_data.get("search_results", [])
        idx = int(parts[2])
        if idx < len(results):
            entry = results[idx]
            ctx.user_data["url"] = entry.get("webpage_url") or entry.get("url", "")
            ctx.user_data["info"] = entry
            await q.message.edit_text(
                f"🎵 *{entry.get('title', '?')}*\n\nChoose download type:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Video", callback_data="dl:video")],
                    [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
                    [InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")],
                ]),
            )

async def show_quality_menu(q, ctx):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL found.")
        return
    heights = await get_all_heights(url)
    # Show only heights >= 360 (but keep if none)
    display_heights = [h for h in heights if h >= 360] or heights
    rows = []
    row = []
    for h in display_heights:
        row.append(InlineKeyboardButton(f"{h}p", callback_data=f"dl:quality:{h}p"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⭐ Best Available", callback_data="dl:quality:best")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await q.message.edit_text("🎬 *Select video quality:*", parse_mode=ParseMode.MARKDOWN,
                              reply_markup=InlineKeyboardMarkup(rows))

async def do_video(q, ctx, uid: int, quality: str):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL.")
        return

    status = await q.message.edit_text(f"⬇️ *Downloading ({quality})…*", parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    hook = build_progress_hook(loop, status, q.message.chat_id, ctx.bot)

    try:
        info = await do_download(url, {"format": quality_to_format(quality)}, hook)
    except (DownloadError, ExtractorError) as e:
        err_str = str(e).lower()
        if "requested format" in err_str or "not available" in err_str:
            await status.edit_text("⚠️ *Quality not available, retrying with best…*", parse_mode=ParseMode.MARKDOWN)
            try:
                info = await do_download(url, {"format": "bestvideo+bestaudio/best"}, hook)
            except Exception as e2:
                await status.edit_text(friendly_error(e2), parse_mode=ParseMode.MARKDOWN)
                return
        else:
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return
    except Exception as e:
        await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        return

    vid_id = info.get("id", "")
    files = (list(DOWNLOAD_DIR.glob(f"{vid_id}.mp4")) or
             list(DOWNLOAD_DIR.glob(f"{vid_id}.mkv")) or
             list(DOWNLOAD_DIR.glob(f"{vid_id}.webm")) or
             list(DOWNLOAD_DIR.glob(f"{vid_id}.*")))
    if not files:
        await status.edit_text("❌ File not found.")
        return

    video_path = str(files[0])
    thumb_path = get_thumbnail_safe(vid_id, info, DOWNLOAD_DIR)

    await status.edit_text("📤 *Uploading video…*", parse_mode=ParseMode.MARKDOWN)

    try:
        with open(video_path, "rb") as vf:
            if thumb_path:
                with open(thumb_path, "rb") as tf:
                    await ctx.bot.send_video(
                        chat_id=q.message.chat_id, video=vf, thumbnail=tf,
                        caption=f"🎬 {info.get('title', '')}\n[{quality}]", supports_streaming=True,
                    )
            else:
                await ctx.bot.send_video(
                    chat_id=q.message.chat_id, video=vf,
                    caption=f"🎬 {info.get('title', '')}\n[{quality}]", supports_streaming=True,
                )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Failed to send video as media: `{e}`\n\nNo document fallback.", parse_mode=ParseMode.MARKDOWN)

    register_for_cleanup(video_path, get_settings(uid)["cleanup_minutes"])
    if thumb_path:
        register_for_cleanup(thumb_path, get_settings(uid)["cleanup_minutes"])

async def do_audio(q, ctx, uid: int):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL.")
        return
    status = await q.message.edit_text("⬇️ *Extracting audio…*", parse_mode=ParseMode.MARKDOWN)
    loop = asyncio.get_event_loop()
    hook = build_progress_hook(loop, status, q.message.chat_id, ctx.bot)
    try:
        info = await do_download(url, {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }, hook)
    except Exception as e:
        await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        return
    vid_id = info.get("id", "")
    files = list(DOWNLOAD_DIR.glob(f"{vid_id}.mp3")) or list(DOWNLOAD_DIR.glob(f"{vid_id}.*"))
    if not files:
        await status.edit_text("❌ Audio not found.")
        return
    filepath = str(files[0])
    await status.edit_text("📤 *Uploading MP3…*", parse_mode=ParseMode.MARKDOWN)
    try:
        with open(filepath, "rb") as f:
            await ctx.bot.send_document(chat_id=q.message.chat_id, document=f,
                                        filename=f"{info.get('title', 'audio')}.mp3",
                                        caption=f"🎵 {info.get('title', '')}")
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
    register_for_cleanup(filepath, get_settings(uid)["cleanup_minutes"])

async def do_thumbnail(q, ctx, uid: int):
    info = ctx.user_data.get("info", {})
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        await q.message.edit_text("❌ No thumbnail.")
        return
    status = await q.message.edit_text("🖼 *Downloading thumbnail…*", parse_mode=ParseMode.MARKDOWN)
    outpath = DOWNLOAD_DIR / f"{info.get('id', 'thumb')}_thumb.jpg"
    try:
        urllib.request.urlretrieve(thumb_url, outpath, timeout=10)
    except Exception as e:
        await status.edit_text(f"❌ Fetch failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        with open(outpath, "rb") as f:
            await ctx.bot.send_document(chat_id=q.message.chat_id, document=f,
                                        filename=f"{info.get('title', 'thumbnail')}.jpg",
                                        caption=f"🖼 {info.get('title', '')}")
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
    register_for_cleanup(str(outpath), get_settings(uid)["cleanup_minutes"])

async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text(f"🔎 Searching: *{query}*…", parse_mode=ParseMode.MARKDOWN)
    try:
        results_info = await extract_info(f"ytsearch5:{query}", download=False, extra_opts={"extract_flat": True})
    except Exception as e:
        await msg.edit_text(f"❌ Search failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    entries = results_info.get("entries", [])
    if not entries:
        await msg.edit_text("😕 No results.")
        return
    ctx.user_data["search_results"] = entries
    buttons = []
    for i, entry in enumerate(entries[:5]):
        title = entry.get("title", "Unknown")[:52]
        dur = entry.get("duration", 0)
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
        buttons.append([InlineKeyboardButton(f"{i+1}. {title} [{dur_str}]", callback_data=f"dl:search:{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await msg.edit_text("🎵 *Top results — tap to select:*", parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup(buttons))

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
        logger.info("✅ cookies.txt OK — %d YT cookies", cs.get("yt_lines", 0))
    else:
        logger.warning("⚠️ cookies.txt problem: %s", cs["reason"])
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cookiecheck", cmd_cookiecheck))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("formats", cmd_formats))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    async def post_init(application: Application):
        await application.bot.set_my_commands([
            BotCommand("start", "Welcome"),
            BotCommand("help", "Help"),
            BotCommand("settings", "Preferences"),
            BotCommand("cookiecheck", "Check cookies"),
            BotCommand("formats", "Show video formats (debug)"),
            BotCommand("stats", "Bot stats"),
        ])
        asyncio.create_task(cleanup_worker())
    app.post_init = post_init
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

# Settings callback (needed)
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(uid))

def settings_keyboard(uid: int) -> InlineKeyboardMarkup:
    s = get_settings(uid)
    mode_lbl = "Fixed ✅" if s["mode"] == "fixed" else "Manual 🎛"
    timer_lbl = "♾ Never" if s["cleanup_minutes"] == 0 else f"{s['cleanup_minutes']} min"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 Default Quality: {s['quality'].upper()}", callback_data="s:quality")],
        [InlineKeyboardButton(f"🔁 Download Mode: {mode_lbl}", callback_data="s:mode")],
        [InlineKeyboardButton(f"🧹 Cleanup Timer: {timer_lbl}", callback_data="s:cleanup")],
        [InlineKeyboardButton("❌ Close", callback_data="s:close")],
    ])

async def settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    parts = q.data.split(":")
    if parts[1] == "close":
        await q.message.delete()
        return
    if parts[1] == "back":
        await q.message.edit_text("⚙️ *Your Settings*", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(uid))
        return
    if parts[1] == "quality" and len(parts) == 2:
        await q.message.edit_text("🎬 *Select Default Quality:*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("360p", callback_data="s:set:quality:360p"),
                 InlineKeyboardButton("480p", callback_data="s:set:quality:480p")],
                [InlineKeyboardButton("720p", callback_data="s:set:quality:720p"),
                 InlineKeyboardButton("1080p", callback_data="s:set:quality:1080p")],
                [InlineKeyboardButton("⭐ Best", callback_data="s:set:quality:best")],
                [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
            ]))
        return
    if parts[1] == "mode" and len(parts) == 2:
        await q.message.edit_text("🔁 *Mode:*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Fixed", callback_data="s:set:mode:fixed")],
                [InlineKeyboardButton("🎛 Manual", callback_data="s:set:mode:manual")],
                [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
            ]))
        return
    if parts[1] == "cleanup" and len(parts) == 2:
        await q.message.edit_text("🧹 *Cleanup Timer:*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("5 min", callback_data="s:set:cleanup:5"),
                 InlineKeyboardButton("10 min", callback_data="s:set:cleanup:10")],
                [InlineKeyboardButton("15 min", callback_data="s:set:cleanup:15"),
                 InlineKeyboardButton("30 min", callback_data="s:set:cleanup:30")],
                [InlineKeyboardButton("♾ Never", callback_data="s:set:cleanup:0")],
                [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
            ]))
        return
    if parts[1] == "set" and len(parts) == 4:
        key, value = parts[2], parts[3]
        s = get_settings(uid)
        if key == "quality": s["quality"] = value
        elif key == "mode": s["mode"] = value
        elif key == "cleanup": s["cleanup_minutes"] = int(value)
        await q.message.edit_text("✅ Saved!", parse_mode=ParseMode.MARKDOWN, reply_markup=settings_keyboard(uid))

if __name__ == "__main__":
    main()