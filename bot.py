"""
Telegram YouTube Downloader Bot – 2 GB uploads, 512 MB RAM friendly
- No video re-encoding (preserves original quality)
- Streaming upload (chunked, never loads file into memory)
- Large files >500 MB sent as documents (avoids ffmpeg)
- Silent audio track added without re-encoding video
"""

import os, asyncio, time, logging, re, threading, urllib.request, sys, platform, subprocess
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

# Pyrogram MTProto client (2 GB uploads)
from pyrogram import Client as PyroClient

TELEGRAM_API_ID   = int(os.environ.get("TELEGRAM_API_ID",  "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
_pyro_bot: "PyroClient | None" = None

# Logging
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN    = os.environ["BOT_TOKEN"]
DOWNLOAD_DIR = Path("downloads")
COOKIES_FILE = "cookies.txt"
DOWNLOAD_DIR.mkdir(exist_ok=True)
BOT_START_TIME = time.time()
YTDL_PROXY = os.environ.get("YTDL_PROXY", "")

DEFAULT_SETTINGS = {"quality": "720p", "mode": "manual", "cleanup_minutes": 10}
user_settings: dict[int, dict] = {}
cleanup_registry: dict[str, float] = {}
_download_sem: asyncio.Semaphore | None = None

def get_download_sem():
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(1)
    return _download_sem

# ---------- Cookies ----------
def init_cookies_from_env():
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return
    try:
        Path(COOKIES_FILE).write_text(raw, encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        logger.info("✅ cookies.txt written (%d lines)", len(lines))
    except Exception as e:
        logger.error("❌ Failed to write cookies.txt: %s", e)

def cookie_status():
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

# ---------- yt-dlp options (no PO token required) ----------
def ydl_opts_base(use_cookies=True):
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "format_sort": ["res", "br"],
        "merge_output_format": "mp4",
        "retries": 10, "fragment_retries": 10, "extractor_retries": 5,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["android_vr", "tv", "tv_downgraded", "web"]}},
    }
    if YTDL_PROXY:
        opts["proxy"] = YTDL_PROXY
    if use_cookies:
        cs = cookie_status()
        if cs["ok"]:
            opts["cookiefile"] = COOKIES_FILE
    return opts

# ---------- Helpers ----------
def get_settings(uid):
    if uid not in user_settings:
        user_settings[uid] = DEFAULT_SETTINGS.copy()
    return user_settings[uid]

def register_for_cleanup(path, minutes):
    cleanup_registry[path] = 0.0 if minutes == 0 else time.time() + minutes * 60

def is_youtube_url(text):
    return bool(re.match(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", text.strip()))

def friendly_error(e):
    msg = str(e).lower()
    if "requested format" in msg:
        return "❌ *Format not available.* Try a different quality."
    if "no video formats" in msg:
        return "❌ *No downloadable formats.* Video may be private."
    if "sign in" in msg or "bot" in msg:
        return "🔒 *YouTube blocks this.* Run /cookiecheck"
    return f"❌ Download failed:\n`{str(e)[:300]}`"

# ---------- Async wrappers ----------
async def extract_info(url, download=False, extra_opts=None):
    opts = ydl_opts_base()
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_running_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download)
    return await loop.run_in_executor(None, _run)

async def do_download_subprocess(url, fmt, out_path, status_msg, loop, label="", extra_args=None):
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
                    pct, total, speed, eta = m.group(1)+"%", m.group(2), m.group(3), m.group(4)
                    now = time.time()
                    if now - _last_edit[0] >= 3:
                        _last_edit[0] = now
                        text = f"⬇️ *Downloading* {label}\n`{pct}` of `{total}` at `{speed}` ETA `{eta}`"
                        asyncio.run_coroutine_threadsafe(
                            status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)
        proc.wait()
        return proc.returncode

    rc = await asyncio.get_running_loop().run_in_executor(None, _parse_and_run)
    if rc != 0:
        raise RuntimeError(f"yt-dlp exited with code {rc}")

    pattern = Path(out_path).stem + ".*"
    candidates = sorted(DOWNLOAD_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No output file for pattern {pattern}")
    return str(candidates[0])

# ---------- Background cleanup ----------
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
                except Exception:
                    pass

# ---------- Health server ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *_): pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever, daemon=True).start()

# ---------- Commands ----------
async def cmd_start(update, ctx):
    await update.message.reply_text(
        "👋 *Welcome to YT Downloader Bot!*\n\n"
        "Send me:\n"
        "• A *YouTube URL* → video / audio / thumbnail\n"
        "• A *song or video name* → search (top 5 results)\n\n"
        "⚙️ /settings – Preferences\n"
        "🍪 /cookiecheck – Diagnose cookies\n"
        "❓ /help – This message",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update, ctx):
    await cmd_start(update, ctx)

async def cmd_cookiecheck(update, ctx):
    cs = cookie_status()
    if not cs["ok"]:
        msg = f"🍪 *Cookie Check* ❌\nIssue: {cs['reason']}\n\nRe-export cookies from youtube.com and redeploy."
    else:
        msg = f"🍪 *Cookie Check* ✅\nYouTube lines: {cs['yt_lines']}\nSAPISID: {'✅' if cs['has_sapisid'] else '⚠️'}"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update, ctx):
    try:
        ytdlp_ver = _yt_dlp_module.version.__version__
    except:
        ytdlp_ver = "unknown"
    ffmpeg_ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0][:50]
    uptime = int(time.time() - BOT_START_TIME)
    h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
    msg = f"📊 *Bot Stats*\nyt-dlp: `{ytdlp_ver}`\nFFmpeg: `{ffmpeg_ver}`\nUptime: `{h}h {m}m {s}s`"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# Settings keyboard (simplified)
def settings_keyboard(uid):
    s = get_settings(uid)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Quality: {s['quality']}", callback_data="s:quality")],
        [InlineKeyboardButton(f"Mode: {'Fixed' if s['mode']=='fixed' else 'Manual'}", callback_data="s:mode")],
        [InlineKeyboardButton("❌ Close", callback_data="s:close")],
    ])

async def cmd_settings(update, ctx):
    await update.message.reply_text("⚙️ Settings", reply_markup=settings_keyboard(update.effective_user.id))

async def settings_callback(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    parts = q.data.split(":")
    if parts[1] == "close":
        await q.message.delete()
        return
    if parts[1] == "quality":
        await q.message.edit_text("Select quality:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("360p", callback_data="s:set:quality:360p"),
             InlineKeyboardButton("720p", callback_data="s:set:quality:720p"),
             InlineKeyboardButton("1080p", callback_data="s:set:quality:1080p")],
            [InlineKeyboardButton("Best", callback_data="s:set:quality:best")],
        ]))
    elif parts[1] == "mode":
        await q.message.edit_text("Mode:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Fixed", callback_data="s:set:mode:fixed"),
             InlineKeyboardButton("Manual", callback_data="s:set:mode:manual")],
        ]))
    elif parts[1] == "set" and len(parts) == 4:
        key, val = parts[2], parts[3]
        s = get_settings(uid)
        if key == "quality": s["quality"] = val
        elif key == "mode": s["mode"] = val
        await q.message.edit_text("✅ Saved", reply_markup=settings_keyboard(uid))

# ---------- Message & callback handlers ----------
async def handle_message(update, ctx):
    text = update.message.text.strip()
    if is_youtube_url(text):
        await handle_youtube_url(update, ctx, text)
    else:
        await handle_search(update, ctx, text)

async def handle_youtube_url(update, ctx, url):
    msg = await update.message.reply_text("🔍 Fetching info...")
    try:
        info = await extract_info(url)
    except Exception as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        return
    title = info.get("title", "Unknown")
    dur = info.get("duration", 0)
    dur_str = f"{dur//60}m {dur%60}s" if dur else "?"
    ctx.user_data["url"] = url
    ctx.user_data["info"] = info
    await msg.edit_text(
        f"📹 *{title}*\n⏱ `{dur_str}`\n\nChoose:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Video", callback_data="dl:video")],
            [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")],
            [InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")],
        ]),
    )

async def download_callback(update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    parts = q.data.split(":")
    action = parts[1]
    if action == "cancel":
        await q.message.edit_text("Cancelled.")
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
                f"🎵 *{entry.get('title','?')}*\nChoose:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Video", callback_data="dl:video")],
                    [InlineKeyboardButton("🎵 Audio", callback_data="dl:audio")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")],
                ]),
            )

async def show_quality_menu(q, ctx):
    info = ctx.user_data.get("info", {})
    formats = info.get("formats", [])
    title = info.get("title", "Video")
    seen = set()
    buttons = []
    for f in formats:
        h = f.get("height")
        if not h or h in seen: continue
        seen.add(h)
        ac = (f.get("acodec") or "none") != "none"
        tag = "🔊" if ac else "🎬"
        buttons.append([InlineKeyboardButton(f"{tag} {h}p", callback_data=f"dl:quality:{h}p")])
    buttons.append([InlineKeyboardButton("⭐ Best", callback_data="dl:quality:best")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await q.message.edit_text(f"🎬 *{title}*\nQuality:", reply_markup=InlineKeyboardMarkup(buttons))

# ---------- MEMORY-SAFE UPLOAD ENGINE (streaming, no re-encode) ----------
def get_video_meta(filepath):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        data = _json.loads(result.stdout)
        streams = data.get("streams", [])
        vs = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return {
            "width": max(0, int(vs.get("width") or 0)),
            "height": max(0, int(vs.get("height") or 0)),
            "duration": max(0, int(float(vs.get("duration") or 0))),
            "has_audio": has_audio,
            "vcodec": vs.get("codec_name", ""),
            "acodec": next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"), ""),
        }
    except:
        return {"width": 0, "height": 0, "duration": 0, "has_audio": False, "vcodec": "", "acodec": ""}

def ensure_telegram_compatible(filepath):
    """
    Minimal ffmpeg operations – no video re-encoding.
    - Add silent audio track if missing (audio only transcoding).
    - Move moov atom to front for streaming.
    - For large files (>500 MB) we skip ffmpeg entirely and send as document.
    """
    file_size = os.path.getsize(filepath)
    if file_size > 500 * 1024 * 1024:
        logger.info("File >500 MB, will send as document – skipping ffmpeg")
        return filepath  # no processing

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
        else:
            logger.warning("Failed to add silent audio: %s", result.stderr[:200])
            return filepath

    # Already has audio: just ensure faststart (stream copy)
    if meta["has_audio"] and (p.suffix.lower() == ".mp4" or True):
        cmd = ["ffmpeg", "-y", "-threads", "1", "-i", filepath, "-c", "copy", "-movflags", "+faststart", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and Path(out_path).exists():
            try: p.unlink()
            except: pass
            return out_path

    return filepath

# Streaming reader (no RAM spike)
import io as _io
class _StreamingFileReader(_io.RawIOBase):
    def __init__(self, filepath, filename, on_progress=None):
        self._path = filepath
        self._name = filename
        self._fh = open(filepath, "rb")
        self._size = os.path.getsize(filepath)
        self._read = 0
        self._progress = on_progress
    @property
    def name(self): return self._name
    def readable(self): return True
    def readinto(self, b):
        chunk = self._fh.read(len(b))
        if not chunk:
            return 0
        n = len(chunk)
        b[:n] = chunk
        self._read += n
        if self._progress:
            try: self._progress(self._read, self._size)
            except: pass
        return n
    def close(self):
        try: self._fh.close()
        except: pass
        try: Path(self._path).unlink(missing_ok=True)
        except: pass
        super().close()

class _NamedBufferedReader(_io.BufferedReader):
    def __init__(self, raw, buffer_size=4*1024*1024):
        super().__init__(raw, buffer_size=buffer_size)
    @property
    def name(self): return self.raw.name

def human_size(b):
    if b < 1024**2: return f"{b/1024:.0f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def progress_bar(pct, width=16): return "█" * round(pct*width/100) + "░" * (width - round(pct*width/100))

def upload_progress_text(name, cur, total, elapsed):
    pct = min(int(cur*100/total), 100) if total else 0
    speed = (cur / elapsed) if elapsed>0 else 0
    eta = ((total - cur) / speed) if speed>0 else -1
    eta_str = f"{int(eta//60)}m {int(eta%60)}s" if eta>0 else "?"
    return f"📤 *Uploading* `{name}`\n`{progress_bar(pct)}` {pct}%\n📦 `{human_size(cur)}` / `{human_size(total)}`\n⚡ `{human_size(speed)}/s`  ⏱ `{eta_str}`"

async def send_file(chat_id, filepath, filename, caption, status_msg, is_video=True, thumb_path=None):
    if _pyro_bot is None or not _pyro_bot.is_connected:
        raise RuntimeError("Pyrogram not connected")
    loop = asyncio.get_running_loop()
    ext = Path(filepath).suffix.lower()
    VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv"}
    AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac"}

    # For large files or non‑video, send as document to avoid ffmpeg
    file_size = os.path.getsize(filepath)
    send_as_doc = False
    if file_size > 500 * 1024 * 1024:
        send_as_doc = True
    elif is_video and ext not in VIDEO_EXTS:
        send_as_doc = True
    elif not is_video and ext not in AUDIO_EXTS:
        send_as_doc = True

    # Apply lightweight compatibility only if sending as video
    if is_video and not send_as_doc:
        filepath = await loop.run_in_executor(None, ensure_telegram_compatible, filepath)
        ext = Path(filepath).suffix.lower()

    _last_edit = [0.0]
    _start = [time.time()]

    def progress_cb(cur, total):
        now = time.time()
        if now - _last_edit[0] >= 3:
            _last_edit[0] = now
            text = upload_progress_text(filename, cur, total, now - _start[0])
            asyncio.run_coroutine_threadsafe(status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)

    await status_msg.edit_text(f"📤 *Uploading* `{filename}` ({human_size(file_size)})…", parse_mode=ParseMode.MARKDOWN)
    raw = _StreamingFileReader(filepath, filename, on_progress=progress_cb)
    bio = _NamedBufferedReader(raw)

    try:
        if send_as_doc:
            await _pyro_bot.send_document(chat_id, bio, caption=caption, file_name=filename)
        elif ext in AUDIO_EXTS:
            await _pyro_bot.send_audio(chat_id, bio, caption=caption, file_name=filename)
        else:
            meta = get_video_meta(filepath)
            await _pyro_bot.send_video(
                chat_id=chat_id, video=bio, caption=caption, file_name=filename,
                width=meta["width"], height=meta["height"], duration=meta["duration"],
                supports_streaming=True, thumb=thumb_path,
            )
    finally:
        bio.close()

# ---------- Download actions ----------
async def do_video(q, ctx, uid, quality):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("No URL")
        return
    sem = get_download_sem()
    if sem.locked():
        await q.message.edit_text("⏳ Another download in progress – queued...")
    async with sem:
        info = ctx.user_data.get("info", {})
        vid_id = info.get("id", "unknown")
        title = info.get("title", vid_id)[:50]

        if quality == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            target = {"360p":360,"720p":720,"1080p":1080,"1440p":1440,"2160p":2160}.get(quality,720)
            fmt = f"bestvideo[height<={target}]+bestaudio/best[height<={target}]/bestvideo+bestaudio/best"

        status = await q.message.edit_text(f"⬇️ Downloading {quality}...", parse_mode=ParseMode.MARKDOWN)
        out_path = str(DOWNLOAD_DIR / f"{vid_id}_{quality}.%(ext)s")
        try:
            merged = await do_download_subprocess(url, fmt, out_path, status, asyncio.get_running_loop(), quality)
        except Exception as e:
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return

        # Thumbnail
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
                    except: return None
                thumb_path = await loop.run_in_executor(None, fetch)

        await status.edit_text("📤 Uploading...", parse_mode=ParseMode.MARKDOWN)
        try:
            await send_file(
                chat_id=q.message.chat_id, filepath=merged, filename=f"{title}.mp4",
                caption=f"🎬 {title} [{quality}]", status_msg=status, is_video=True, thumb_path=thumb_path,
            )
            await status.delete()
        except Exception as e:
            await status.edit_text(f"Upload failed: {e}", parse_mode=ParseMode.MARKDOWN)
        finally:
            if thumb_path:
                Path(thumb_path).unlink(missing_ok=True)

async def do_audio(q, ctx, uid):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("No URL")
        return
    sem = get_download_sem()
    async with sem:
        status = await q.message.edit_text("⬇️ Extracting audio...", parse_mode=ParseMode.MARKDOWN)
        out_path = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")
        try:
            audio_path = await do_download_subprocess(
                url, "bestaudio[ext=m4a]/bestaudio/best", out_path, status,
                asyncio.get_running_loop(), "audio",
                extra_args=["--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K"]
            )
        except Exception as e:
            await status.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
            return
        title = Path(audio_path).stem
        await status.edit_text("📤 Uploading MP3...", parse_mode=ParseMode.MARKDOWN)
        try:
            await send_file(q.message.chat_id, audio_path, f"{title}.mp3", f"🎵 {title}", status, is_video=False)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"Upload failed: {e}", parse_mode=ParseMode.MARKDOWN)

async def do_thumbnail(q, ctx, uid):
    info = ctx.user_data.get("info", {})
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        await q.message.edit_text("No thumbnail")
        return
    status = await q.message.edit_text("🖼 Downloading thumbnail...", parse_mode=ParseMode.MARKDOWN)
    out = DOWNLOAD_DIR / f"{info.get('id','thumb')}.jpg"
    loop = asyncio.get_running_loop()
    def fetch():
        try: urllib.request.urlretrieve(thumb_url, out); return out.exists()
        except: return False
    if await loop.run_in_executor(None, fetch):
        with open(out, "rb") as f:
            await q.message.reply_document(f, filename=f"{info.get('title','thumb')}.jpg")
        await status.delete()
        register_for_cleanup(str(out), get_settings(uid)["cleanup_minutes"])
    else:
        await status.edit_text("Failed to fetch thumbnail")

async def handle_search(update, ctx, query):
    msg = await update.message.reply_text(f"🔎 Searching: {query}...", parse_mode=ParseMode.MARKDOWN)
    try:
        results = await extract_info(f"ytsearch5:{query}", extra_opts={"extract_flat": True})
    except Exception as e:
        await msg.edit_text(f"Search failed: {e}")
        return
    entries = results.get("entries", [])
    if not entries:
        await msg.edit_text("No results")
        return
    ctx.user_data["search_results"] = entries
    buttons = []
    for i, e in enumerate(entries[:5]):
        title = e.get("title", "?")[:40]
        dur = e.get("duration", 0)
        dur_str = f"{dur//60}:{dur%60:02d}" if dur else "?"
        buttons.append([InlineKeyboardButton(f"{i+1}. {title} [{dur_str}]", callback_data=f"dl:search:{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await msg.edit_text("Top results:", reply_markup=InlineKeyboardMarkup(buttons))

# ---------- Error handler ----------
async def error_handler(update, ctx):
    logger.exception("Unhandled error: %s", ctx.error)
    try:
        if update and update.callback_query:
            await update.callback_query.message.edit_text("⚠️ Unexpected error, please retry.")
    except: pass

# ---------- Pyrogram lifecycle ----------
async def start_pyro_bot():
    global _pyro_bot
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
    _pyro_bot = PyroClient("yt_bot", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH,
                           bot_token=BOT_TOKEN, no_updates=True)
    await _pyro_bot.start()
    me = await _pyro_bot.get_me()
    logger.info("✅ Pyrogram connected as @%s", me.username or "?")

async def stop_pyro_bot():
    global _pyro_bot
    if _pyro_bot and _pyro_bot.is_connected:
        await _pyro_bot.stop()

# ---------- Main ----------
def main():
    init_cookies_from_env()
    cs = cookie_status()
    logger.info("Cookie status: %s", "OK" if cs["ok"] else cs.get("reason", "missing"))
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

    async def post_init(application):
        global _download_sem
        _download_sem = asyncio.Semaphore(1)
        await application.bot.set_my_commands([
            BotCommand("start", "Start"), BotCommand("help", "Help"),
            BotCommand("settings", "Settings"), BotCommand("cookiecheck", "Check cookies"),
            BotCommand("stats", "Bot stats"),
        ])
        await start_pyro_bot()
        asyncio.create_task(cleanup_worker())

    async def post_shutdown(application):
        await stop_pyro_bot()

    app.post_init = post_init
    app.post_shutdown = post_shutdown
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()