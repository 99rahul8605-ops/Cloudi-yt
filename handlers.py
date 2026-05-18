"""
handlers.py — All Telegram command, message, and callback query handlers.

URL flow (fully automatic — no menus):
  • Image post  → detect → download image → send
  • Video post  → detect → download best quality → send
  YouTube uses 1080p, all other platforms use best available.

YouTube search (text input):
  → show top 5 results → user taps one → auto-download immediately
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from yt_dlp.utils import DownloadError, ExtractorError

import config
from config import (
    DOWNLOAD_DIR, get_settings, register_for_cleanup,
    user_settings, cleanup_registry, BOT_START_TIME,
    TELEGRAM_API_ID, TELEGRAM_API_HASH,
)
from cookies import (
    youtube_cookie_status, facebook_cookie_status, instagram_cookie_status,
)
from platforms import detect_platform, is_supported_url, PLATFORM_EMOJI
from downloader import extract_info, download_video
from uploader import send_file, download_thumbnail
from utils import (
    friendly_error, format_uptime, download_dir_info,
    get_ffmpeg_version, get_ytdlp_version,
)

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Media Downloader Bot!*\n\n"
        "Just send a link — I'll download it automatically!\n\n"
        "▶️ YouTube  📸 Instagram  👥 Facebook\n"
        "📌 Pinterest  🎵 TikTok  🐦 Twitter/X  🟠 Reddit\n"
        "🌐 Vimeo, Dailymotion, and 1000+ more\n\n"
        "Or send a *song/video name* to search YouTube.\n\n"
        "⚙️ /settings  🍪 /cookiecheck  📊 /stats",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ *Help*\n\n"
        "Paste any URL → download starts automatically.\n"
        "• YouTube: 1080p\n"
        "• All other platforms: best available quality\n\n"
        "Send any *text* (not a URL) to search YouTube.\n\n"
        "For private/age-restricted content, set cookie env vars:\n"
        "`YOUTUBE_COOKIES`, `IG_COOKIES`, `FB_COOKIES`\n"
        "Run /cookiecheck to verify.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cookiecheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    yt = youtube_cookie_status()
    fb = facebook_cookie_status()
    ig = instagram_cookie_status()

    def _line(cs, label):
        return (f"✅ {label}: `{cs.get('yt_lines', cs.get('total','?'))}` lines"
                if cs["ok"] else f"❌ {label}: {cs['reason']}")

    msg = (
        "🍪 *Cookie Status*\n\n"
        f"{_line(yt,'YouTube')}\n{_line(ig,'Instagram')}\n{_line(fb,'Facebook')}\n\n"
    )
    if not yt["ok"]:
        msg += ("*Fix YouTube:* Export from `youtube.com` → set `YOUTUBE_COOKIES` env var\n\n")
    if not ig["ok"] and not fb["ok"]:
        msg += "*Fix IG/FB:* Export → set `IG_COOKIES` or `FB_COOKIES` env var"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    file_count, dir_bytes = download_dir_info()
    pyro       = config._pyro_bot
    upload_str = ("🚀 2 GB ✅ (Pyrogram MTProto)"
                  if (pyro and pyro.is_connected) else "❌ Pyrogram not connected")
    msg = (
        "📊 *Bot Statistics*\n\n"
        f"• yt-dlp:  `{get_ytdlp_version()}`\n"
        f"• FFmpeg:  `{get_ffmpeg_version()}`\n"
        f"• Python:  `{sys.version.split()[0]}`\n"
        f"• Uptime:  `{format_uptime(time.time() - BOT_START_TIME)}`\n"
        f"• Downloads: `{file_count}` files / `{dir_bytes/1024**2:.1f} MB`\n"
        f"• Upload: {upload_str}\n"
        f"• YouTube cookies: {'✅' if youtube_cookie_status()['ok'] else '❌'}\n"
        f"• Instagram cookies: {'✅' if instagram_cookie_status()['ok'] else '❌'}\n"
        f"• Facebook cookies: {'✅' if facebook_cookie_status()['ok'] else '❌'}\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS  (cleanup timer only)
# ═════════════════════════════════════════════════════════════════════════════

def _settings_keyboard(uid: int) -> InlineKeyboardMarkup:
    s         = get_settings(uid)
    timer_lbl = "♾ Never" if s["cleanup_minutes"] == 0 else f"{s['cleanup_minutes']} min"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🧹 Auto-Cleanup: {timer_lbl}", callback_data="s:cleanup")],
        [InlineKeyboardButton("❌ Close",                       callback_data="s:close")],
    ])


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=_settings_keyboard(update.effective_user.id),
    )


async def settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; uid = q.from_user.id; await q.answer()
    parts = q.data.split(":")
    if parts[1] == "close":
        await q.message.delete(); return
    if parts[1] == "back":
        await q.message.edit_text("⚙️ *Settings*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=_settings_keyboard(uid)); return
    if parts[1] == "cleanup" and len(parts) == 2:
        await q.message.edit_text(
            "🧹 *Auto-Cleanup Timer:*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("5 min",   callback_data="s:set:cleanup:5"),
                 InlineKeyboardButton("10 min",  callback_data="s:set:cleanup:10")],
                [InlineKeyboardButton("15 min",  callback_data="s:set:cleanup:15"),
                 InlineKeyboardButton("30 min",  callback_data="s:set:cleanup:30")],
                [InlineKeyboardButton("♾ Never", callback_data="s:set:cleanup:0")],
                [InlineKeyboardButton("⬅️ Back",  callback_data="s:back")],
            ])); return
    if parts[1] == "set" and len(parts) == 4:
        s = get_settings(uid)
        if parts[2] == "cleanup":
            s["cleanup_minutes"] = int(parts[3])
        await q.message.edit_text("✅ *Saved!*", parse_mode=ParseMode.MARKDOWN,
            reply_markup=_settings_keyboard(uid))


# ═════════════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ═════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_supported_url(text):
        await handle_url(update, ctx, text)
    else:
        await handle_search(update, ctx, text)


# ═════════════════════════════════════════════════════════════════════════════
#  URL HANDLER — fully automatic, no menus
# ═════════════════════════════════════════════════════════════════════════════

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    """Detect content type and download immediately — no prompts."""
    platform = detect_platform(url)
    emoji    = PLATFORM_EMOJI.get(platform, "🌐")
    msg      = await update.message.reply_text(
        f"{emoji} *Fetching info…*", parse_mode=ParseMode.MARKDOWN)

    try:
        info = await extract_info(url)
    except (DownloadError, ExtractorError) as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return
    except Exception as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN); return

    if not info:
        await msg.edit_text("❌ Could not fetch info.", parse_mode=ParseMode.MARKDOWN)
        return

    uid      = update.effective_user.id
    title    = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    vid_id   = info.get("id", url.rstrip("/").split("/")[-1])

    slim_formats = [
        {k: f.get(k) for k in
         ("format_id", "ext", "height", "width", "vcodec", "acodec",
          "filesize", "filesize_approx", "format_note", "tbr", "fps")}
        for f in (info.get("formats") or [])
    ]
    cached_info = {
        "id": vid_id, "title": title, "duration": duration,
        "thumbnail":  info.get("thumbnail"),
        "thumbnails": [{"url": t.get("url"), "width": t.get("width")}
                       for t in (info.get("thumbnails") or []) if t.get("url")],
        "formats": slim_formats,
    }

    has_video = any(
        (f.get("vcodec") or "none").lower() not in ("none", "")
        for f in slim_formats
    )
    has_img_ext = any(
        (f.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp", "gif")
        for f in slim_formats
    )
    is_image = (not has_video and not duration) or has_img_ext

    if is_image:
        await _download_image(msg, ctx, url, platform, cached_info, uid)
    else:
        quality = "1080p" if platform == "youtube" else "best"
        await _download_video(msg, url, platform, cached_info, uid, quality)


# ─────────────────────────────────────────────────────────────────────────────
#  Auto image download
# ─────────────────────────────────────────────────────────────────────────────

async def _download_image(msg, ctx, url, platform, info, uid):
    emoji  = PLATFORM_EMOJI.get(platform, "🌐")
    title  = info.get("title", "Image post")
    vid_id = info.get("id", "img")
    s      = get_settings(uid)

    await msg.edit_text("⬇️ *Downloading image…*", parse_mode=ParseMode.MARKDOWN)

    downloaded_files: list[str] = []

    # Step 1: thumbnail URL (fastest — no extra network call)
    thumb_url  = None
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        best      = sorted(thumbnails, key=lambda t: t.get("width") or 0, reverse=True)
        thumb_url = best[0].get("url")
    if not thumb_url:
        thumb_url = info.get("thumbnail")

    if thumb_url:
        outpath = str(DOWNLOAD_DIR / f"{vid_id}_img.jpg")
        try:
            urllib.request.urlretrieve(thumb_url, outpath)
            if Path(outpath).stat().st_size > 2000:
                downloaded_files = [outpath]
                logger.info("Image via thumbnail URL: %s", outpath)
        except Exception as e:
            logger.warning("Thumbnail URL failed: %s", e)

    # Step 2: writethumbnail via yt-dlp (IG photos that have no pre-known URL)
    if not downloaded_files:
        loop = asyncio.get_event_loop()
        try:
            from yt_dlp import YoutubeDL
            from platforms import ydl_opts_for
            opts = ydl_opts_for(url)
            opts.update({
                "skip_download":  True,
                "writethumbnail": True,
                "outtmpl": str(DOWNLOAD_DIR / f"{vid_id}_%(autonumber)s.%(ext)s"),
            })
            def _get_thumb():
                before = set(DOWNLOAD_DIR.glob(f"{vid_id}_*"))
                try:
                    with YoutubeDL(opts) as ydl:
                        ydl.extract_info(url, download=True)
                except Exception:
                    pass
                after = set(DOWNLOAD_DIR.glob(f"{vid_id}_*"))
                return [str(f) for f in (after - before)
                        if f.suffix.lower() in (".jpg",".jpeg",".png",".webp",".gif")]
            downloaded_files = await loop.run_in_executor(None, _get_thumb)
            if downloaded_files:
                logger.info("Image via writethumbnail: %s", downloaded_files)
        except Exception as e:
            logger.warning("writethumbnail failed: %s", e)

    if not downloaded_files:
        await msg.edit_text(
            "❌ Could not download image.\nThe post may be private or require login.",
            parse_mode=ParseMode.MARKDOWN)
        return

    await msg.edit_text(
        f"📤 *Sending {len(downloaded_files)} image(s)…*", parse_mode=ParseMode.MARKDOWN)

    caption = f"{emoji} *{title}*"
    sent    = 0
    for fpath in sorted(downloaded_files):
        try:
            with open(fpath, "rb") as f:
                await ctx.bot.send_photo(chat_id=msg.chat_id, photo=f,
                                         caption=caption if sent == 0 else "")
            sent += 1
        except Exception:
            try:
                with open(fpath, "rb") as f:
                    await ctx.bot.send_document(chat_id=msg.chat_id, document=f,
                                                filename=Path(fpath).name,
                                                caption=caption if sent == 0 else "")
                sent += 1
            except Exception as e2:
                logger.warning("Failed to send image %s: %s", fpath, e2)
        register_for_cleanup(fpath, s["cleanup_minutes"])

    if sent:
        await msg.delete()
    else:
        await msg.edit_text("❌ Failed to send image.", parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────────────────────
#  Auto video download
# ─────────────────────────────────────────────────────────────────────────────

async def _download_video(msg, url, platform, cached_info, uid, quality):
    emoji  = PLATFORM_EMOJI.get(platform, "🌐")
    title  = cached_info.get("title", "video")
    vid_id = cached_info.get("id", "unknown")
    s      = get_settings(uid)

    await msg.edit_text(
        f"⬇️ *Downloading* {emoji} `{title}`…", parse_mode=ParseMode.MARKDOWN)

    try:
        merged_path = await download_video(url, quality, msg, vid_id, cached_info)
    except Exception as e:
        await msg.edit_text(friendly_error(e), parse_mode=ParseMode.MARKDOWN)
        gc.collect(); return

    gc.collect()
    thumb_path = download_thumbnail(cached_info, vid_id)
    if thumb_path:
        register_for_cleanup(thumb_path, s["cleanup_minutes"])

    safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip()
    caption    = f"{emoji} *{title}*"

    try:
        await send_file(
            chat_id    = msg.chat_id,
            filepath   = merged_path,
            filename   = f"{safe_title}.mp4",
            caption    = caption,
            status_msg = msg,
            is_video   = True,
            thumb_path = thumb_path,
        )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Upload failed: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    register_for_cleanup(merged_path, s["cleanup_minutes"])


# ═════════════════════════════════════════════════════════════════════════════
#  YOUTUBE SEARCH — shows results list, auto-downloads on tap
# ═════════════════════════════════════════════════════════════════════════════

async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    msg = await update.message.reply_text(
        f"🔎 *Searching:* `{query}`…", parse_mode=ParseMode.MARKDOWN)
    try:
        results_info = await extract_info(
            f"ytsearch5:{query}", download=False,
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
        t   = entry.get("title", "Unknown")[:52]
        dur = entry.get("duration", 0)
        ds  = f"{dur//60}:{dur%60:02d}" if dur else "?"
        buttons.append([InlineKeyboardButton(f"{i+1}. {t} [{ds}]",
                                             callback_data=f"dl:search:{i}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dl:cancel")])
    await msg.edit_text(
        "🎵 *Tap to download:*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  CALLBACKS  (search selection + settings)
# ═════════════════════════════════════════════════════════════════════════════

async def download_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    uid    = q.from_user.id
    await q.answer()
    parts  = q.data.split(":")
    action = parts[1]

    if action == "cancel":
        await q.message.edit_text("❌ Cancelled."); return

    if action == "search" and len(parts) == 3:
        results = ctx.user_data.get("search_results", [])
        idx     = int(parts[2])
        if idx >= len(results):
            await q.message.edit_text("❌ Result no longer available."); return

        entry  = results[idx]
        url    = entry.get("webpage_url") or entry.get("url", "")
        vid_id = entry.get("id", "unknown")
        cached_info = {
            "id": vid_id,
            "title":      entry.get("title", "video"),
            "duration":   entry.get("duration", 0),
            "thumbnail":  entry.get("thumbnail"),
            "thumbnails": [],
            "formats":    [],
        }
        await _download_video(q.message, url, "youtube", cached_info, uid, "1080p")


# ═════════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER
# ═════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    import traceback
    tb = "".join(traceback.format_exception(
        type(ctx.error), ctx.error, ctx.error.__traceback__))
    logger.error("Unhandled exception:\n%s", tb)
    short = str(ctx.error)[:400]
    try:
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.message.edit_text(
                f"⚠️ *Error:*\n`{short}`", parse_mode=ParseMode.MARKDOWN)
        elif isinstance(update, Update) and update.message:
            await update.message.reply_text(
                f"⚠️ *Error:*\n`{short}`", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
