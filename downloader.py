"""
downloader.py — yt-dlp async wrappers, format selection helpers, and ffmpeg merge.
"""

import asyncio
import gc
import logging
import subprocess
from pathlib import Path

import json as _json
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError
from telegram.constants import ParseMode

from config import DOWNLOAD_DIR
from platforms import ydl_opts_for, ydl_opts_youtube, detect_platform
from utils import download_progress_text, build_progress_hook

logger = logging.getLogger(__name__)


# ── Core async wrappers ───────────────────────────────────────────────────────

async def extract_info(url: str, download: bool = False,
                       extra_opts: dict | None = None) -> dict:
    """Extract video metadata (and optionally download) with platform-correct options."""
    opts = ydl_opts_for(url)
    if extra_opts:
        opts.update(extra_opts)
    loop = asyncio.get_event_loop()
    def _run():
        # For Instagram/Pinterest, suppress yt-dlp's stderr noise entirely.
        # These platforms produce various expected errors (image posts, private
        # accounts, followers-only content) that we handle gracefully — we don't
        # want yt-dlp's raw ERROR lines polluting the logs.
        _suppress_platforms = ("instagram.com", "pinterest.com", "pin.it")
        _is_image_platform = any(p in url for p in _suppress_platforms)
        if _is_image_platform:
            opts["ignore_no_formats_error"] = True
            opts["quiet"] = True
            opts["no_warnings"] = False  # keep warnings for our logger
            # Redirect yt-dlp's logger to suppress ERROR lines from reaching stderr
            import logging as _logging
            class _SilentLogger:
                def debug(self, msg): pass
                def info(self, msg): pass
                def warning(self, msg): logger.warning("yt-dlp: %s", msg)
                def error(self, msg): logger.debug("yt-dlp error (suppressed): %s", msg)
            opts["logger"] = _SilentLogger()

        _partial: list[dict] = []

        class _CapturingYDL(YoutubeDL):
            def process_ie_result(self, ie_result, *args, **kwargs):
                _partial.append(dict(ie_result))
                return super().process_ie_result(ie_result, *args, **kwargs)

        try:
            with _CapturingYDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            if info:
                fmts = info.get("formats") or []
                if not fmts and _is_image_platform:
                    # ignore_no_formats_error returned info with no formats — image post
                    info.setdefault("duration", 0)
                    logger.info("Image post detected (no formats) for %s", url[:60])
                elif fmts:
                    exts    = sorted({f.get("ext") for f in fmts if f.get("ext")})
                    heights = sorted({f.get("height") for f in fmts
                                      if isinstance(f.get("height"), int) and f["height"] > 0})
                    logger.info("Formats available — exts: %s | heights: %s", exts, heights)
            return info
        except (DownloadError, ExtractorError) as e:
            err_lower = str(e).lower()
            if ("no video formats" in err_lower or "no formats" in err_lower
                    or "no video in this post" in err_lower
                    or "there is no video" in err_lower):
                if _partial:
                    partial = _partial[-1]
                    partial["formats"] = []
                    partial.setdefault("duration", 0)
                    logger.info("Returning partial info (image post) for %s", url[:60])
                    return partial
            raise

    return await loop.run_in_executor(None, _run)


async def do_download(url: str, extra_opts: dict, progress_cb) -> dict:
    """Download with a yt-dlp progress hook callback."""
    opts = ydl_opts_for(url)
    opts.update(extra_opts)
    opts["progress_hooks"] = [progress_cb]
    loop = asyncio.get_event_loop()
    def _run():
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fmts = info.get("formats", []) if info else []
            if fmts:
                summary = [(f.get("format_id"), f.get("ext"), f.get("height"),
                            f.get("vcodec","?")[:6], f.get("acodec","?")[:6])
                           for f in fmts[-10:]]
                logger.info("Available formats (last 10): %s", summary)
            return info
    return await loop.run_in_executor(None, _run)


# ── Format selection ──────────────────────────────────────────────────────────

def pick_best_formats(formats: list, quality: str) -> tuple[str, str]:
    """
    Return (video_selector, audio_selector) yt-dlp format strings.
    Uses selector strings (not raw IDs) to avoid "format not available" errors
    when format IDs change between extraction and download sessions.
    """
    target_h = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080,
                "1440p": 1440, "2160p": 2160, "4k": 2160}.get(quality)

    video_only = [f for f in formats
                  if (f.get("vcodec") or "none") != "none"
                  and (f.get("acodec") or "none") == "none"]
    audio_only = [f for f in formats
                  if (f.get("acodec") or "none") != "none"
                  and (f.get("vcodec") or "none") == "none"]
    muxed      = [f for f in formats
                  if (f.get("vcodec") or "none") != "none"
                  and (f.get("acodec") or "none") != "none"]

    logger.info("Format buckets — video-only: %d  audio-only: %d  muxed: %d",
                len(video_only), len(audio_only), len(muxed))
    if not video_only and muxed:
        logger.warning("⚠️ No adaptive streams — only %d muxed format(s).", len(muxed))

    if not target_h or quality == "best":
        return "bestvideo", "bestaudio"

    vid_sel = f"bestvideo[height<={target_h}]"
    aud_sel = "bestaudio"
    logger.info("Selector for %s: video=%s  audio=%s", quality, vid_sel, aud_sel)
    return vid_sel, aud_sel


def build_format_selector(quality: str) -> str:
    """Build a single combined yt-dlp format selector string for a quality."""
    if quality == "best":
        return "bestvideo+bestaudio/best"
    target_h = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080,
                "1440p": 1440, "2160p": 2160, "4k": 2160}.get(quality, 1080)
    return (
        f"bestvideo[height<={target_h}]+bestaudio"
        f"/best[height<={target_h}]"
        f"/bestvideo+bestaudio"
        f"/best"
    )


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

async def ffmpeg_merge(video_path: str, audio_path: str, out_path: str) -> None:
    """
    Merge a video-only file and an audio-only file into a single mp4.
    Runs in a thread-pool executor so it doesn't block the event loop.
    NOTE: No -movflags +faststart — that forces a full moov rewrite which
    takes minutes on large files. Not needed for Telegram uploads.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        out_path,
    ]
    logger.info("ffmpeg merge: %s + %s → %s", video_path, audio_path, out_path)
    loop = asyncio.get_event_loop()
    def _run():
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-800:])
    await loop.run_in_executor(None, _run)


# ── Full video download flow ──────────────────────────────────────────────────

async def download_video(url: str, quality: str, status_msg,
                         vid_id: str, cached_info: dict) -> str:
    """
    Download video+audio for the given URL and quality.
    Returns the path to the final merged file.
    Raises on error (caller should handle and edit status_msg).
    """
    fmt        = build_format_selector(quality)
    loop       = asyncio.get_event_loop()
    base_opts  = ydl_opts_for(url)
    out_path   = str(DOWNLOAD_DIR / f"{vid_id}_{quality}.%(ext)s")

    def _download() -> str:
        last = [0.0]
        import time, asyncio as _asyncio
        def hook(d):
            import time as _t
            if d["status"] != "downloading": return
            now = _t.time()
            if now - last[0] < 3: return
            last[0] = now
            pct   = d.get("_percent_str",  "0%").strip()
            speed = d.get("_speed_str",    "?").strip()
            eta   = d.get("_eta_str",      "?").strip()
            down  = d.get("_downloaded_bytes_str", "?").strip()
            total = (d.get("_total_bytes_str") or
                     d.get("_total_bytes_estimate_str") or "?")
            total = total.strip() if isinstance(total, str) else "?"
            text  = download_progress_text(f"*{quality}*", pct, speed, eta, down, total)
            _asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)

        opts = {
            **base_opts,
            "format":              fmt,
            "format_sort":         ["res", "vcodec:h264", "acodec:m4a", "br"],
            "merge_output_format": "mp4",
            "outtmpl":             out_path,
            "progress_hooks":      [hook],
            "quiet":               False,
            "no_warnings":         False,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                logger.info("Selected: %s | height=%s | vcodec=%s",
                            info.get("format","?"), info.get("height","?"),
                            info.get("vcodec","?"))

        found = sorted(DOWNLOAD_DIR.glob(f"{vid_id}_{quality}.*"))
        if not found:
            raise FileNotFoundError(f"No output file found for {vid_id}")
        return str(found[-1])

    merged_path = await loop.run_in_executor(None, _download)
    gc.collect()
    return merged_path
