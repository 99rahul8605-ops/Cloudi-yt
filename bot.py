"""
Advanced Telegram YouTube Downloader Bot – No Quality Loss
- Downloads best available video+audio (no codec restrictions)
- Never re-encodes video (preserves original quality)
- Non-H264 videos sent as documents (original bitstream)
- Silent audio track added without re-encoding video
"""

import os, asyncio, time, logging, re, threading, urllib.request, sys, platform, subprocess, gc
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

from pyrogram import Client as PyroClient

# ---------- Configuration ----------
TELEGRAM_API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
BOT_TOKEN         = os.environ["BOT_TOKEN"]
DOWNLOAD_DIR      = Path("downloads")
COOKIES_FILE      = "cookies.txt"
DOWNLOAD_DIR.mkdir(exist_ok=True)
BOT_START_TIME    = time.time()
YTDL_PROXY        = os.environ.get("YTDL_PROXY", "")

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Pyrogram client ----------
_pyro_bot: "PyroClient | None" = None

# ---------- User settings ----------
DEFAULT_SETTINGS = {"quality": "720p", "mode": "manual", "cleanup_minutes": 10}
user_settings: dict[int, dict] = {}
cleanup_registry: dict[str, float] = {}

# ---------- Semaphore (only 1 download at a time) ----------
_download_sem: asyncio.Semaphore | None = None

def get_download_sem() -> asyncio.Semaphore:
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(1)
    return _download_sem

# ========== COOKIE HELPERS ==========
def init_cookies_from_env() -> None:
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return
    try:
        Path(COOKIES_FILE).write_text(raw, encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        logger.info("✅ cookies.txt written (%d lines)", len(lines))
    except Exception as e:
        logger.error("❌ Failed to write cookies.txt: %s", e)

def cookie_status() -> dict:
    path = Path(COOKIES_FILE)
    if not path.exists():
        return {"ok": False, "reason": "File not found"}
    if path.stat().st_size < 100:
        return {"ok": False, "reason": "File too small"}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as e:
        return {"ok": False, "reason": f"Cannot read: {e}"}
    real = [l for l in lines if l.strip() and not l.startswith("#")]
    yt = [l for l in real if "youtube.com" in l or "google.com" in l]
    if not yt:
        return {"ok": False, "reason": "No youtube.com cookies"}
    return {"ok": True, "yt_lines": len(yt), "has_sapisid": any("SAPISID" in l for l in yt)}

# ========== YT-DLP OPTIONS (no PO token, no codec restrictions) ==========
def ydl_opts_base(use_cookies: bool = True) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        # NO codec restrictions – get absolute best video+audio
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "tv", "tv_downgraded", "web"],
            }
        },
    }
    if YTDL_PROXY:
        opts["proxy"] = YTDL_PROXY
        logger.info("Using proxy: %s", YTDL_PROXY)
    if use_cookies:
        cs = cookie_status()
        if cs["ok"]:
            opts["cookiefile"] = COOKIES_FILE
            logger.info("cookies.txt loaded (%d YT lines)", cs.get("yt_lines", 0))
        else:
            logger.warning("cookies.txt problem: %s", cs["reason"])
    return opts

# ========== HELPERS ==========
def get_settings(uid: int) -> dict:
    if uid not in user_settings:
        user_settings[uid] = DEFAULT_SETTINGS.copy()
    return user_settings[uid]

def register_for_cleanup(path: str, minutes: int):
    cleanup_registry[path] = 0.0 if minutes == 0 else time.time() + minutes * 60

def is_youtube_url(text: str) -> bool:
    return bool(re.match(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", text.strip()))

def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "requested format" in msg:
        return "❌ *Format not available.* Try a different quality."
    if "no video formats" in msg:
        return "❌ *No downloadable formats.* Video may be private."
    if "sign in" in msg or "bot" in msg:
        return "🔒 *YouTube is blocking this.* Run /cookiecheck"
    return f"❌ Download failed:\n`{str(e)[:300]}`"

def human_size(b: int) -> str:
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def _progress_bar(pct: int, width=16) -> str:
    filled = round(pct * width / 100)
    return "█" * filled + "░" * (width - filled)

def _speed_str(bps: float) -> str:
    if bps <= 0: return "?"
    if bps >= 1024**3: return f"{bps/1024**3:.1f} GB/s"
    if bps >= 1024**2: return f"{bps/1024**2:.1f} MB/s"
    return f"{bps/1024:.0f} KB/s"

def _eta_str(seconds: float) -> str:
    if seconds < 0: return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:   return f"{h}h {m}m"
    if m:   return f"{m}m {s:02d}s"
    return f"{s}s"

def upload_progress_text(filename: str, current: int, total: int, elapsed: float) -> str:
    pct = min(int(current * 100 / total), 100) if total else 0
    bar = _progress_bar(pct)
    done = human_size(current)
    tot = human_size(total)
    spd = _speed_str(current / elapsed if elapsed > 0 else 0)
    eta_sec = ((total - current) / (current / elapsed)) if current > 0 and elapsed > 0 else -1
    eta = _eta_str(eta_sec)
    return f"📤 *Uploading* `{filename}`\n`{bar}` {pct}%\n📦 `{done}` / `{tot}`\n⚡ `{spd}`  ⏱ `{eta}`"

def download_progress_text(label: str, pct: int, speed: str, eta: str, downloaded: str, total: str) -> str:
    bar = _progress_bar(pct)
    tot = f" / `{total}`" if total and total != "?" else ""
    return f"⬇️ *Downloading* {label}\n`{bar}` {pct}%\n📦 `{downloaded}`{tot}\n⚡ `{speed}`  ⏱ `{eta}`"

# ========== FFMPEG HELPERS (NO RE-ENCODING) ==========
def get_video_meta(filepath: str) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        data = _json.loads(result.stdout)
        streams = data.get("streams", [])
        vs = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        dur_str = vs.get("duration") or "0"
        return {
            "width": max(0, int(vs.get("width") or 0)),
            "height": max(0, int(vs.get("height") or 0)),
            "duration": max(0, int(float(dur_str))),
            "has_audio": has_audio,
            "vcodec": vs.get("codec_name", ""),
            "acodec": next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"), ""),
        }
    except Exception:
        return {"width": 0, "height": 0, "duration": 0, "has_audio": False, "vcodec": "", "acodec": ""}

def ensure_telegram_compatible(filepath: str) -> str:
    """
    ONLY add silent audio track (if missing) and move moov atom.
    NEVER re-encode video. If video is not H.264, it will be sent as document later.
    """
    file_size = os.path.getsize(filepath)
    if file_size > 500 * 1024 * 1024:
        logger.info("File >500 MB – skipping ffmpeg, will send as document")
        return filepath

    p = Path(filepath)
    meta = get_video_meta(filepath)
    out_path = str(p.parent / (p.stem + "_tg.mp4"))

    # If no audio, add silent track (copy video, encode audio only)
    if not meta["has_audio"]:
        cmd = [
            "ffmpeg", "-y", "-threads", "1",
            "-i", filepath,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            out_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode == 0 and Path(out_path).exists():
            try: p.unlink()
            except: pass
            return out_path
        logger.warning("Failed to add silent audio: %s", result.stderr[:200])
        return filepath

    # Already has audio: ensure faststart (stream copy) – this is lossless
    cmd = ["ffmpeg", "-y", "-threads", "1", "-i", filepath, "-c", "copy", "-movflags", "+faststart", out_path]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode == 0 and Path(out_path).exists():
        try: p.unlink()
        except: pass
        return out_path

    return filepath

# ========== UPLOAD (direct file path – Pyrogram streams internally) ==========
async def send_file(
    chat_id: int,
    filepath: str,
    filename: str,
    caption: str,
    status_msg,
    is_video: bool = True,
    thumb_path: str | None = None,
) -> None:
    if _pyro_bot is None or not _pyro_bot.is_connected:
        raise RuntimeError("Pyrogram not connected")
    loop = asyncio.get_running_loop()
    ext = Path(filepath).suffix.lower()
    VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv"}
    AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac"}

    file_size = os.path.getsize(filepath)
    meta = get_video_meta(filepath) if is_video else {}
    vcodec = meta.get("vcodec", "")
    is_h264 = vcodec in ("h264", "avc", "avc1")

    # Decide to send as document:
    # 1. File > 500 MB
    # 2. Not a recognised video extension
    # 3. Video codec is not H.264 (to avoid Telegram re-encoding which damages quality)
    send_as_doc = False
    if file_size > 500 * 1024 * 1024:
        send_as_doc = True
    elif is_video and ext not in VIDEO_EXTS:
        send_as_doc = True
    elif is_video and not is_h264:
        logger.info("Video codec is %s – sending as document to preserve quality", vcodec)
        send_as_doc = True
    elif not is_video and ext not in AUDIO_EXTS:
        send_as_doc = True

    # Apply compatibility only if sending as video and not as doc
    if is_video and not send_as_doc:
        filepath = await loop.run_in_executor(None, ensure_telegram_compatible, filepath)
        ext = Path(filepath).suffix.lower()

    _last_edit = [0.0]
    _start = [time.time()]

    async def progress_cb(current: int, total: int):
        now = time.time()
        if now - _last_edit[0] >= 3:
            _last_edit[0] = now
            elapsed = now - _start[0]
            text = upload_progress_text(filename, current, total, elapsed)
            await status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)

    await status_msg.edit_text(f"📤 *Preparing upload* `{filename}` ({human_size(file_size)})…", parse_mode=ParseMode.MARKDOWN)

    try:
        if send_as_doc:
            await _pyro_bot.send_document(chat_id, filepath, caption=caption, file_name=filename, progress=progress_cb)
        elif ext in AUDIO_EXTS:
            await _pyro_bot.send_audio(chat_id, filepath, caption=caption, file_name=filename, progress=progress_cb)
        else:
            meta = get_video_meta(filepath)
            await _pyro_bot.send_video(
                chat_id=chat_id, video=filepath, caption=caption, file_name=filename,
                width=meta["width"], height=meta["height"], duration=meta["duration"],
                supports_streaming=True, thumb=thumb_path, progress=progress_cb,
            )
    finally:
        # Delete file after upload
        try:
            Path(filepath).unlink(missing_ok=True)
        except:
            pass
        if thumb_path:
            Path(thumb_path).unlink(missing_ok=True)
        gc.collect()
        logger.info("Upload finished, file deleted: %s", filename)

# ========== DOWNLOAD CORE (subprocess, low memory) ==========
async def do_download_subprocess(
    url: str,
    fmt: str,
    out_path: str,
    status_msg,
    loop,
    label: str = "",
    extra_args: list | None = None,
) -> None:
    opts = ydl_opts_base()
    cmd = ["yt-dlp", "--no-playlist", "-f", fmt,
           "--merge-output-format", "mp4",
           "--output", out_path,
           "--newline", "--progress", "--no-warnings"]
    if opts.get("cookiefile") and Path(opts["cookiefile"]).exists():
        cmd += ["--cookies", opts["cookiefile"]]
    if opts.get("proxy"):
        cmd += ["--proxy", opts["proxy"]]
    cmd += ["--extractor-args", "youtube:player_client=android_vr,tv,tv_downgraded,web"]
    if extra_args:
        cmd += extra_args
    cmd.append(url)

    logger.info("yt-dlp subprocess: %s", " ".join(cmd))
    _last_edit = [0.0]

    def _parse_and_run():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if "[download]" in line and "%" in line:
                m = re.search(r"([\d.]+)%\s+of\s+([\S]+)\s+at\s+([\S]+)\s+ETA\s+(\S+)", line)
                if m:
                    pct_str, total, speed, eta = m.group(1), m.group(2), m.group(3), m.group(4)
                    try:
                        pct = int(float(pct_str))
                    except:
                        pct = 0
                    now = time.time()
                    if now - _last_edit[0] >= 3:
                        _last_edit[0] = now
                        text = download_progress_text(label, pct, speed, eta, "?", total)
                        asyncio.run_coroutine_threadsafe(
                            status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop
                        )
        proc.wait()
        return proc.returncode

    rc = await asyncio.get_running_loop().run_in_executor(None, _parse_and_run)
    if rc != 0:
        raise RuntimeError(f"yt-dlp exited with code {rc}")

# ========== BACKGROUND CLEANUP ==========
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
                except Exception:
                    pass

# ========== HEALTH SERVER ==========
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *_): pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever, daemon=True).start()

# ========== COMMANDS ==========
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
        msg = f"🍪 *Cookie Check* ❌\nIssue: {cs['reason']}\n\nRe-export cookies from youtube.com and redeploy."
    else:
        msg = f"🍪 *Cookie Check* ✅\nYouTube lines: {cs['yt_lines']}\nSAPISID: {'✅' if cs['has_sapisid'] else '⚠️'}"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ytdlp_ver = _yt_dlp_module.version.__version__
    except:
        ytdlp_ver = "unknown"
    ffmpeg_ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0][:50]
    uptime = int(time.time() - BOT_START_TIME)
    h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
    msg = f"📊 *Bot Stats*\nyt-dlp: `{ytdlp_ver}`\nFFmpeg: `{ffmpeg_ver}`\nUptime: `{h}h {m}m {s}s`"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ========== SETTINGS ==========
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
                [InlineKeyboardButton("360p", callback_data="s:set:quality:360p"),
                 InlineKeyboardButton("480p", callback_data="s:set:quality:480p")],
                [InlineKeyboardButton("720p", callback_data="s:set:quality:720p"),
                 InlineKeyboardButton("1080p", callback_data="s:set:quality:1080p")],
                [InlineKeyboardButton("🟣 1440p (2K)", callback_data="s:set:quality:1440p"),
                 InlineKeyboardButton("🔵 2160p (4K)", callback_data="s:set:quality:2160p")],
                [InlineKeyboardButton("⭐ Best Available", callback_data="s:set:quality:best")],
                [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
            ])); return
    if parts[1] == "mode" and len(parts) == 2:
        await q.message.edit_text("🔁 *Download Mode:*\n\n• *Fixed* – always use default quality\n• *Manual* – choose quality per download",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Fixed Quality", callback_data="s:set:mode:fixed")],
                [InlineKeyboardButton("🎛 Manual Selection", callback_data="s:set:mode:manual")],
                [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
            ])); return
    if parts[1] == "cleanup" and len(parts) == 2:
        await q.message.edit_text("🧹 *Auto-Cleanup Timer:*\nFiles deleted after this delay.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("5 min", callback_data="s:set:cleanup:5"),
                 InlineKeyboardButton("10 min", callback_data="s:set:cleanup:10")],
                [InlineKeyboardButton("15 min", callback_data="s:set:cleanup:15"),
                 InlineKeyboardButton("30 min", callback_data="s:set:cleanup:30")],
                [InlineKeyboardButton("♾ Never", callback_data="s:set:cleanup:0")],
                [InlineKeyboardButton("⬅️ Back", callback_data="s:back")],
            ])); return
    if parts[1] == "set" and len(parts) == 4:
        key, value = parts[2], parts[3]
        s = get_settings(uid)
        if key == "quality": s["quality"] = value
        elif key == "mode": s["mode"] = value
        elif key == "cleanup": s["cleanup_minutes"] = int(value)
        await q.message.edit_text("✅ *Setting saved!*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=settings_keyboard(uid))

# ========== MESSAGE & CALLBACK HANDLERS ==========
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
    info = ctx.user_data.get("info", {})
    formats = info.get("formats", [])
    title = info.get("title", "Video")
    seen_heights = set()
    buttons = []
    for f in formats:
        h = f.get("height")
        if not h or h in seen_heights: continue
        seen_heights.add(h)
        ac = (f.get("acodec") or "none") != "none"
        tag = "🔊" if ac else "🎬"
        buttons.append([InlineKeyboardButton(f"{tag} {h}p", callback_data=f"dl:quality:{h}p")])
    buttons.append([InlineKeyboardButton("⭐ Best Available", callback_data="dl:quality:best")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await q.message.edit_text(f"🎬 *{title}*\n\nSelect quality:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def extract_info(url: str, download: bool = False, extra_opts: dict | None = None) -> dict:
    opts = ydl_opts_base()
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_running_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)
    return await loop.run_in_executor(None, _run)

# ========== VIDEO DOWNLOAD ==========
async def do_video(q, ctx, uid: int, quality: str):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored.")
        return
    sem = get_download_sem()
    if sem.locked():
        await q.message.edit_text("⏳ *Another download is in progress. You're queued — please wait…*", parse_mode=ParseMode.MARKDOWN)
    async with sem:
        info = ctx.user_data.get("info", {})
        vid_id = info.get("id", "unknown")
        title = info.get("title", vid_id)[:50]

        # Build format selector – NO codec restrictions, get best available
        if quality == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            target_h = {"360p":360, "480p":480, "720p":720, "1080p":1080, "1440p":1440, "2160p":2160}.get(quality, 1080)
            # Use height cap but no codec filtering
            fmt = f"bestvideo[height<={target_h}]+bestaudio/best[height<={target_h}]/bestvideo+bestaudio/best"

        status = await q.message.edit_text(f"⬇️ *Downloading ({quality})…*", parse_mode=ParseMode.MARKDOWN)
        out_path = str(DOWNLOAD_DIR / f"{vid_id}_{quality}.%(ext)s")
        try:
            await do_download_subprocess(url, fmt, out_path, status, asyncio.get_running_loop(), quality)
        except Exception as e:
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return

        # Find downloaded file
        found = sorted(DOWNLOAD_DIR.glob(f"{vid_id}_{quality}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            await status.edit_text("❌ No output file found after download.")
            return
        merged_path = str(found[0])

        # Thumbnail download
        thumb_path = None
        thumbs = info.get("thumbnails") or []
        if thumbs:
            best = max(thumbs, key=lambda t: t.get("width", 0))
            thumb_url = best.get("url")
            if thumb_url:
                loop = asyncio.get_running_loop()
                def fetch():
                    out = DOWNLOAD_DIR / f"{vid_id}_thumb.jpg"
                    try:
                        urllib.request.urlretrieve(thumb_url, out)
                        return str(out) if out.exists() else None
                    except:
                        return None
                thumb_path = await loop.run_in_executor(None, fetch)

        await status.edit_text("📤 *Uploading…*", parse_mode=ParseMode.MARKDOWN)
        try:
            await send_file(
                chat_id=q.message.chat_id, filepath=merged_path, filename=f"{title}.mp4",
                caption=f"🎬 {title} [{quality}]", status_msg=status, is_video=True, thumb_path=thumb_path,
            )
            await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        finally:
            if thumb_path:
                Path(thumb_path).unlink(missing_ok=True)
            # merged_path is deleted inside send_file

# ========== AUDIO DOWNLOAD ==========
async def do_audio(q, ctx, uid: int):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored.")
        return
    sem = get_download_sem()
    if sem.locked():
        await q.message.edit_text("⏳ *Another download is in progress. You're queued — please wait…*", parse_mode=ParseMode.MARKDOWN)
    async with sem:
        status = await q.message.edit_text("⬇️ *Extracting audio…*", parse_mode=ParseMode.MARKDOWN)
        out_path = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")
        try:
            await do_download_subprocess(
                url,
                "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
                out_path,
                status,
                asyncio.get_running_loop(),
                "audio",
                extra_args=["--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K"]
            )
        except Exception as e:
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return

        # Find mp3 file
        files = sorted(DOWNLOAD_DIR.glob("*.mp3"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            await status.edit_text("❌ Audio file not found.")
            return
        filepath = str(files[0])
        title = files[0].stem
        await status.edit_text("📤 *Uploading MP3…*", parse_mode=ParseMode.MARKDOWN)
        try:
            await send_file(q.message.chat_id, filepath, f"{title}.mp3", f"🎵 {title}", status, is_video=False)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        finally:
            Path(filepath).unlink(missing_ok=True)

# ========== THUMBNAIL ==========
async def do_thumbnail(q, ctx, uid: int):
    info = ctx.user_data.get("info", {})
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        await q.message.edit_text("❌ No thumbnail found.")
        return
    status = await q.message.edit_text("🖼 *Downloading thumbnail…*", parse_mode=ParseMode.MARKDOWN)
    outpath = DOWNLOAD_DIR / f"{info.get('id', 'thumb')}_thumb.jpg"
    try:
        urllib.request.urlretrieve(thumb_url, outpath)
    except Exception as e:
        await status.edit_text(f"❌ Thumbnail fetch failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        with open(outpath, "rb") as f:
            await q.message.reply_document(f, filename=f"{info.get('title', 'thumbnail')}.jpg")
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    register_for_cleanup(str(outpath), get_settings(uid)["cleanup_minutes"])

# ========== SEARCH ==========
async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text(f"🔎 Searching: *{query}*…", parse_mode=ParseMode.MARKDOWN)
    try:
        results_info = await extract_info(f"ytsearch5:{query}", extra_opts={"extract_flat": True})
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
        title = entry.get("title", "Unknown")[:52]
        dur = entry.get("duration", 0)
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
        buttons.append([InlineKeyboardButton(f"{i+1}. {title} [{dur_str}]", callback_data=f"dl:search:{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await msg.edit_text("🎵 *Top results — tap to select:*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

# ========== ERROR HANDLER ==========
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    import traceback
    tb = "".join(traceback.format_exception(type(ctx.error), ctx.error, ctx.error.__traceback__))
    logger.error("Unhandled exception:\n%s", tb)
    short = str(ctx.error)[:400]
    msg = f"⚠️ *Unexpected error:*\n`{short}`"
    try:
        if update and update.callback_query:
            await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif update and update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

# ========== PYROGRAM LIFECYCLE ==========
async def start_pyro_bot() -> None:
    global _pyro_bot
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
    _pyro_bot = PyroClient("yt_bot", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH,
                           bot_token=BOT_TOKEN, no_updates=True)
    await _pyro_bot.start()
    me = await _pyro_bot.get_me()
    logger.info("✅ Pyrogram connected as @%s", me.username or "?")

async def stop_pyro_bot() -> None:
    global _pyro_bot
    if _pyro_bot and _pyro_bot.is_connected:
        await _pyro_bot.stop()

# ========== MAIN ==========
def main():
    init_cookies_from_env()
    cs = cookie_status()
    if cs["ok"]:
        logger.info("✅ cookies.txt OK — %d YouTube lines", cs.get("yt_lines", 0))
    else:
        logger.warning("⚠️ cookies.txt problem: %s", cs["reason"])
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cookiecheck", cmd_cookiecheck))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    async def post_init(application: Application):
        global _download_sem
        _download_sem = asyncio.Semaphore(1)
        await application.bot.set_my_commands([
            BotCommand("start", "Welcome"), BotCommand("help", "Help"),
            BotCommand("settings", "Preferences"), BotCommand("cookiecheck", "Cookie status"),
            BotCommand("stats", "Bot info"),
        ])
        await start_pyro_bot()
        asyncio.create_task(cleanup_worker())

    async def post_shutdown(application: Application):
        await stop_pyro_bot()

    app.post_init = post_init
    app.post_shutdown = post_shutdown
    logger.info("Bot started – polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()