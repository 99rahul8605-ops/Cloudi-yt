"""
uploader.py — Pyrogram MTProto upload engine (2 GB limit).

Features:
  • Live upload progress bar every 3 s
  • ffprobe metadata injection (width/height/duration)
  • Audio-codec fix (opus→aac) via ffmpeg — video never re-encoded
  • Silent-audio-track patch so Telegram never converts videos to GIFs
  • Thumbnail injection for the Telegram video player
"""

import asyncio
import json as _json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from pyrogram import Client as PyroClient
from telegram.constants import ParseMode

import config
from config import (
    BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH,
    DOWNLOAD_DIR,
)
from utils import human_size, upload_progress_text

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".opus"}


# ── Pyrogram client lifecycle ─────────────────────────────────────────────────

async def start_pyro_bot() -> None:
    """Create and start the Pyrogram bot client (called from post_init)."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are required.\n"
            "Get them at https://my.telegram.org/apps"
        )
    config._pyro_bot = PyroClient(
        name       = "yt_dl_bot",
        api_id     = TELEGRAM_API_ID,
        api_hash   = TELEGRAM_API_HASH,
        bot_token  = BOT_TOKEN,
        no_updates = True,
    )
    await config._pyro_bot.start()
    me = await config._pyro_bot.get_me()
    logger.info("✅ Pyrogram MTProto ready — @%s (2 GB upload limit)", me.username or "?")


async def stop_pyro_bot() -> None:
    """Disconnect Pyrogram on shutdown (called from post_shutdown)."""
    if config._pyro_bot and config._pyro_bot.is_connected:
        await config._pyro_bot.stop()
        logger.info("Pyrogram client stopped.")


# ── FFprobe / FFmpeg helpers ──────────────────────────────────────────────────

def get_video_meta(filepath: str) -> dict:
    """Return width, height, duration(s), has_audio via ffprobe."""
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


def fix_audio_only(filepath: str) -> str:
    """
    Fix audio codec if incompatible with Telegram (opus/vorbis → aac).
    Video stream is NEVER re-encoded — only audio is transcoded.

    No -movflags +faststart: that flag rewrites the entire file to relocate
    the moov atom, which takes minutes on large files. Omitting it keeps this
    step to ~5-15 seconds regardless of video resolution or file size.
    Telegram uploads work fine without faststart.
    """
    try:
        p = Path(filepath)
        if not p.exists():
            return filepath

        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", filepath],
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

        # Already compatible
        if acodec in ("aac", "mp3", ""):
            logger.info("[FFMPEG] audio already compatible, uploading directly")
            return filepath

        out_path = str(p.parent / (p.stem + "_fix.mp4"))
        timeout  = max(120, int(src_mb * 2))

        if not as_:
            # No audio — add silent AAC track so Telegram doesn't convert to GIF
            cmd = [
                "ffmpeg", "-y", "-i", filepath,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                out_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", filepath,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                out_path,
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


def _needs_audio_fix(filepath: str) -> bool:
    """Quick ffprobe check — returns True if audio codec needs transcoding."""
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


def download_thumbnail(info: dict, vid_id: str) -> str | None:
    """Download the best available thumbnail to a local JPEG. Returns path or None."""
    thumbnails = info.get("thumbnails") or []
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
            if Path(out_path).stat().st_size > 1000:
                logger.info("Thumbnail saved: %s", out_path)
                return out_path
        except Exception as e:
            logger.debug("Thumbnail attempt failed (%s): %s", url[:60], e)
    return None


# ── Unified upload helper ─────────────────────────────────────────────────────

async def send_file(
    chat_id:    int,
    filepath:   str,
    filename:   str,
    caption:    str,
    status_msg,
    is_video:   bool = True,
    thumb_path: str | None = None,
) -> None:
    """
    Upload a file via Pyrogram MTProto (2 GB limit).
    Shows live progress, fixes audio codec if needed, injects ffprobe metadata.
    """
    if config._pyro_bot is None or not config._pyro_bot.is_connected:
        raise RuntimeError("Pyrogram client is not running.")

    filepath  = str(filepath)
    file_size = os.path.getsize(filepath)
    ext       = Path(filepath).suffix.lower()

    logger.info("Uploading %s (%.1f MB) via Pyrogram MTProto", filename,
                file_size / 1024**2)

    # ── Progress callback ─────────────────────────────────────────────────────
    _last_edit:  list[float] = [0.0]
    _start_time: list[float] = [time.time()]

    async def _progress(current: int, total: int) -> None:
        now = time.time()
        if now - _last_edit[0] < 5:
            return
        _last_edit[0] = now
        elapsed = now - _start_time[0]
        text = upload_progress_text(filename, current, total, elapsed)
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            err = str(e).lower()
            if "retry" in err or "flood" in err:
                import re as _re
                m = _re.search(r"retry.*?(\d+)", err)
                backoff = int(m.group(1)) if m else 10
                _last_edit[0] = now + backoff
            elif "message is not modified" in err:
                pass
            else:
                logger.debug("Upload progress edit skipped: %s", e)

    # ── Video upload ──────────────────────────────────────────────────────────
    if is_video and ext in VIDEO_EXTS:
        loop = asyncio.get_event_loop()

        needs_fix = await loop.run_in_executor(None, _needs_audio_fix, filepath)
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

        filepath = await loop.run_in_executor(None, fix_audio_only, filepath)

        if needs_fix:
            await status_msg.edit_text(
                f"📤 *Uploading* `{filename}` *({human_size(os.path.getsize(filepath))})*…",
                parse_mode=ParseMode.MARKDOWN,
            )

        meta = get_video_meta(filepath)
        logger.info("Video meta — %dx%d  dur=%ds  has_audio=%s",
                    meta["width"], meta["height"], meta["duration"], meta["has_audio"])

        await config._pyro_bot.send_video(
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

    # ── Photo upload ─────────────────────────────────────────────────────────
    # Pyrogram uses MTProto (not Bot API) so send_photo sends full HD resolution.
    elif ext in {".jpg", ".jpeg", ".png", ".webp"}:
        await status_msg.edit_text(
            f"📤 *Uploading* `{filename}` *({human_size(file_size)})*…",
            parse_mode=ParseMode.MARKDOWN,
        )
        await config._pyro_bot.send_photo(
            chat_id  = chat_id,
            photo    = filepath,
            caption  = caption,
            progress = _progress,
        )

    # ── Audio upload ──────────────────────────────────────────────────────────
    elif ext in AUDIO_EXTS:
        await status_msg.edit_text(
            f"📤 *Uploading* `{filename}` *({human_size(file_size)})*…",
            parse_mode=ParseMode.MARKDOWN,
        )
        await config._pyro_bot.send_audio(
            chat_id   = chat_id,
            audio     = filepath,
            caption   = caption,
            file_name = filename,
            progress  = _progress,
        )

    # ── Document fallback ─────────────────────────────────────────────────────
    else:
        await status_msg.edit_text(
            f"📤 *Uploading* `{filename}` *({human_size(file_size)})*…",
            parse_mode=ParseMode.MARKDOWN,
        )
        await config._pyro_bot.send_document(
            chat_id   = chat_id,
            document  = filepath,
            caption   = caption,
            file_name = filename,
            progress  = _progress,
        )
