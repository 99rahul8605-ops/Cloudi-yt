"""
handlers.py — All Telegram command, message, and callback query handlers.

Supported commands:
  /start   /help   /settings   /cookiecheck   /stats

Message handler:
  • YouTube / Instagram / Facebook / Pinterest / TikTok / Twitter / Reddit URL → download flow
  • Any other text → YouTube search (top 5 results)

Download flow (inline keyboard):
  Video → quality picker → download + upload
  Audio → MP3 extract + upload
  Thumbnail → fetch + send
"""

import asyncio
import gc
import logging
import re
import sys
import platform as _platform
import time
import urllib.request
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from yt_dlp.utils import DownloadError, ExtractorError

import config
from config import (
    DOWNLOAD_DIR, get_settings, register_for_cleanup,
    user_settings, cleanup_registry, BOT_START_TIME, _pyro_bot,
    TELEGRAM_API_ID, TELEGRAM_API_HASH,
)
from cookies import (
    youtube_cookie_status, facebook_cookie_status, instagram_cookie_status,
    COOKIES_FILE, FB_COOKIES_FILE, IG_COOKIES_FILE,
)
from platforms import detect_platform, platform_label, is_supported_url, PLATFORM_EMOJI
from downloader import (
    extract_info, do_download, pick_best_formats, download_video,
)
from uploader import send_file, download_thumbnail
from utils import (
    friendly_error, human_size, format_uptime, download_dir_info,
    get_ffmpeg_version, get_ytdlp_version, download_progress_text,
)

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Media Downloader Bot!*\n\n"
        "Send me a link from any of these platforms:\n\n"
        "▶️ *YouTube* — videos, shorts, playlists\n"
        "📸 *Instagram* — reels, posts, stories\n"
        "👥 *Facebook* — videos, reels, watch\n"
        "📌 *Pinterest* — video pins\n"
        "🎵 *TikTok* — public videos\n"
        "🐦 *Twitter / X* — videos, GIFs\n"
        "🟠 *Reddit* — video posts\n"
        "🌐 *Other sites* — Vimeo, Dailymotion, etc.\n\n"
        "Or send a *song/video name* to search YouTube.\n\n"
        "⚙️ /settings – Preferences\n"
        "🍪 /cookiecheck – Cookie status\n"
        "📊 /stats – Bot info\n"
        "❓ /help – Help",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Help & Usage*\n\n"
        "*Downloading a video:*\n"
        "1. Paste a URL from YouTube, Instagram, TikTok, etc.\n"
        "2. Choose Video, Audio MP3, or Thumbnail\n"
        "3. Select quality (Video only)\n\n"
        "*YouTube Search:*\n"
        "Send any text (not a URL) to search YouTube.\n\n"
        "*Cookie setup (for private/age-restricted content):*\n"
        "• YouTube: set `YOUTUBE_COOKIES` env var\n"
        "• Instagram/Facebook: set `IG_COOKIES` or `FB_COOKIES` env var\n"
        "• Run /cookiecheck to verify\n\n"
        "*Supported sites:*\n"
        "YouTube, Instagram, Facebook, Pinterest, TikTok, Twitter/X, "
        "Reddit, Vimeo, Dailymotion, and 1000+ more via yt-dlp.\n\n"
        "⚙️ /settings — Change quality, mode, cleanup timer",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookiecheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yt = youtube_cookie_status()
    fb = facebook_cookie_status()
    ig = instagram_cookie_status()

    def _status_line(cs: dict, label: str) -> str:
        if cs["ok"]:
            return f"✅ {label}: `{cs.get('yt_lines', cs.get('total', '?'))}` cookie lines"
        return f"❌ {label}: {cs['reason']}"

    msg = (
        "🍪 *Cookie Status*\n\n"
        f"{_status_line(yt, 'YouTube')}\n"
        f"{_status_line(ig, 'Instagram')}\n"
        f"{_status_line(fb, 'Facebook')}\n\n"
    )

    if not yt["ok"]:
        msg += (
            "*Fix YouTube cookies:*\n"
            "1. Export cookies from `youtube.com` (logged in)\n"
            "2. Use *'Get cookies.txt LOCALLY'* extension\n"
            "3. Set `YOUTUBE_COOKIES` env var to file contents\n"
            "⚠️ Do NOT export in incognito mode\n\n"
        )
    if not ig["ok"] and not fb["ok"]:
        msg += (
            "*Fix Instagram/Facebook cookies:*\n"
            "1. Export cookies from `instagram.com` or `facebook.com`\n"
            "2. Set `IG_COOKIES` or `FB_COOKIES` env var\n"
            "Note: public content works without cookies\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ytdlp_ver          = get_ytdlp_version()
    ffmpeg_ver         = get_ffmpeg_version()
    python_ver         = sys.version.split()[0]
    os_info            = f"{_platform.system()} {_platform.release()}"
    uptime_str         = format_uptime(time.time() - BOT_START_TIME)
    file_count, dir_bytes = download_dir_info()
    dir_mb             = dir_bytes / (1024 * 1024)
    active_users       = len(user_settings)
    queued_files       = len(cleanup_registry)

    yt_cs  = youtube_cookie_status()
    fb_cs  = facebook_cookie_status()
    ig_cs  = instagram_cookie_status()

    pyro = config._pyro_bot
    if pyro and pyro.is_connected:
        upload_str = "🚀 2 GB ✅ (Pyrogram MTProto)"
    elif TELEGRAM_API_ID and TELEGRAM_API_HASH:
        upload_str = "⚠️ Credentials set but Pyrogram not connected"
    else:
        upload_str = "❌ TELEGRAM_API_ID / TELEGRAM_API_HASH not set"

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
        f"  • Files: `{file_count}`  Size: `{dir_mb:.2f} MB`\n\n"
        f"📤 *Upload engine:* {upload_str}\n\n"
        "🍪 *Cookies*\n"
        f"  • YouTube:   {'✅' if yt_cs['ok'] else '❌'}\n"
        f"  • Instagram: {'✅' if ig_cs['ok'] else '❌'}\n"
        f"  • Facebook:  {'✅' if fb_cs['ok'] else '❌'}\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════════════════════

def _settings_keyboard(uid: int) -> InlineKeyboardMarkup:
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
    await update.message.reply_text(
        "⚙️ *Your Settings*\nTap an option to change it:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_settings_keyboard(uid),
    )


async def settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; uid = q.from_user.id; await q.answer()
    parts = q.data.split(":")

    if parts[1] == "close":
        await q.message.delete(); return
    if parts[1] == "back":
        await q.message.edit_text("⚙️ *Your Settings*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=_settings_keyboard(uid)); return

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
            reply_markup=_settings_keyboard(uid))


# ═════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER — URL or search query
# ═════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_supported_url(text):
        await handle_url(update, ctx, text)
    else:
        await handle_search(update, ctx, text)


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    platform = detect_platform(url)
    label    = platform_label(url)
    msg = await update.message.reply_text(f"{label} — 🔍 Fetching info…")

    try:
        info = await extract_info(url)
    except (DownloadError, ExtractorError) as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return
    except Exception as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return

    title    = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    dur_str  = f"{duration // 60}m {duration % 60}s" if duration else "?"

    ctx.user_data["url"]      = url
    ctx.user_data["platform"] = platform

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
        "formats": slim_formats,
    }

    emoji = PLATFORM_EMOJI.get(platform, "🌐")
    has_formats = bool(slim_formats)

    buttons = [
        [InlineKeyboardButton("🎬 Video",     callback_data="dl:video")],
        [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
    ]
    if info.get("thumbnail"):
        buttons.append([InlineKeyboardButton("🖼 Thumbnail", callback_data="dl:thumb")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])

    await msg.edit_text(
        f"{emoji} *{title}*\n⏱ `{dur_str}`\n\nWhat would you like?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD CALLBACKS
# ═════════════════════════════════════════════════════════════════════════════

async def download_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; uid = q.from_user.id; await q.answer()
    parts  = q.data.split(":")
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
            ctx.user_data["url"]      = entry.get("webpage_url") or entry.get("url", "")
            ctx.user_data["platform"] = "youtube"
            ctx.user_data["info"]     = entry
            await q.message.edit_text(
                f"🎵 *{entry.get('title', '?')}*\n\nChoose download type:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Video",     callback_data="dl:video")],
                    [InlineKeyboardButton("🎵 Audio MP3", callback_data="dl:audio")],
                    [InlineKeyboardButton("❌ Cancel",    callback_data="dl:cancel")],
                ]),
            )


async def show_quality_menu(q, ctx):
    info    = ctx.user_data.get("info", {})
    formats = info.get("formats", [])
    title   = info.get("title", "Video")

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
            f"{tag} {note}{size_str}", callback_data=f"dl:quality:{h}p")])

    buttons.append([InlineKeyboardButton("⭐ Best Available", callback_data="dl:quality:best")])
    buttons.append([InlineKeyboardButton("❌ Cancel",         callback_data="dl:cancel")])

    await q.message.edit_text(
        f"🎬 *{title}*\n\nSelect quality:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Video ─────────────────────────────────────────────────────────────────────

async def do_video(q, ctx, uid: int, quality: str):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored. Please resend the link."); return

    cached_info = ctx.user_data.get("info", {})
    vid_id      = cached_info.get("id", "unknown")
    title       = cached_info.get("title", vid_id)
    platform    = ctx.user_data.get("platform", detect_platform(url))
    emoji       = PLATFORM_EMOJI.get(platform, "🌐")

    status = await q.message.edit_text(
        f"⬇️ *Downloading {emoji} ({quality})…*",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        merged_path = await download_video(url, quality, status, vid_id, cached_info)
        logger.info("Downloaded: %s", merged_path)
    except Exception as e:
        raw = str(e)[:500]
        logger.error("Download failed: %s", raw)
        await status.edit_text(
            friendly_error(e) + f"\n\n`{raw}`",
            parse_mode=ParseMode.MARKDOWN)
        return
    finally:
        gc.collect()

    thumb_path = download_thumbnail(cached_info, vid_id)
    s = get_settings(uid)
    if thumb_path:
        register_for_cleanup(thumb_path, s["cleanup_minutes"])

    safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip()
    filename   = f"{safe_title}_{quality}.mp4"
    caption    = f"{emoji} *{title}*\n🎞 Quality: `{quality}`"

    try:
        await send_file(
            chat_id    = q.message.chat_id,
            filepath   = merged_path,
            filename   = filename,
            caption    = caption,
            status_msg = status,
            is_video   = True,
            thumb_path = thumb_path,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    finally:
        ctx.user_data.pop("info", None)
        gc.collect()

    register_for_cleanup(merged_path, s["cleanup_minutes"])


# ── Audio ─────────────────────────────────────────────────────────────────────

async def do_audio(q, ctx, uid: int):
    url = ctx.user_data.get("url")
    if not url:
        await q.message.edit_text("❌ No URL stored."); return

    platform = ctx.user_data.get("platform", detect_platform(url))
    emoji    = PLATFORM_EMOJI.get(platform, "🌐")
    status   = await q.message.edit_text(
        f"⬇️ *Extracting audio {emoji}…*", parse_mode=ParseMode.MARKDOWN)

    loop = asyncio.get_event_loop()
    from utils import build_progress_hook
    hook = build_progress_hook(loop, status, "🎵 audio")
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
    title    = info.get("title", "audio")
    try:
        await send_file(
            chat_id    = q.message.chat_id,
            filepath   = filepath,
            filename   = f"{title}.mp3",
            caption    = f"🎵 {title}",
            status_msg = status,
            is_video   = False,
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return

    register_for_cleanup(filepath, get_settings(uid)["cleanup_minutes"])


# ── Thumbnail ─────────────────────────────────────────────────────────────────

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
                chat_id  = q.message.chat_id,
                document = f,
                filename = f"{info.get('title', 'thumbnail')}.jpg",
                caption  = f"🖼 {info.get('title', '')}",
            )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN); return

    register_for_cleanup(str(outpath), get_settings(uid)["cleanup_minutes"])


# ═════════════════════════════════════════════════════════════════════════════
#  YOUTUBE SEARCH
# ═════════════════════════════════════════════════════════════════════════════

async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text(
        f"🔎 Searching YouTube: *{query}*…", parse_mode=ParseMode.MARKDOWN)
    try:
        results_info = await extract_info(
            f"ytsearch5:{query}",
            download=False,
            extra_opts={"extract_flat": True},
        )
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

    await msg.edit_text(
        "🎵 *Top results — tap to select:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER
# ═════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    import traceback
    tb = "".join(traceback.format_exception(type(ctx.error), ctx.error,
                                             ctx.error.__traceback__))
    logger.error("Unhandled exception:\n%s", tb)
    short = str(ctx.error)[:400]
    msg   = f"⚠️ *Unexpected error:*\n`{short}`"
    try:
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
        elif isinstance(update, Update) and update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
