"""
platforms.py — Platform detection and per-platform yt-dlp option builders.

Supported platforms:
  • YouTube       — android_vr/tv client chain, no PO token required
  • Instagram     — Reels, posts, stories (cookies strongly recommended)
  • Facebook      — Videos, Reels, Watch (cookies required for private content)
  • Pinterest     — Video pins
  • TikTok        — Videos (no login needed for public content)
  • Twitter / X   — Videos, GIFs
  • Reddit        — Video posts (v.redd.it + cross-posts)
  • Generic       — Any other yt-dlp-supported site (Vimeo, Dailymotion, etc.)
"""

import logging
import random
import re
from pathlib import Path

from config import YTDL_PROXY, DOWNLOAD_DIR, USER_AGENTS
from cookies import (
    youtube_cookie_status, best_cookie_file_for, COOKIES_FILE,
    FB_COOKIES_FILE, IG_COOKIES_FILE,
)

logger = logging.getLogger(__name__)


# ── Platform detection ────────────────────────────────────────────────────────

_PLATFORM_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("youtube",    re.compile(
        r"(https?://)?(www\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/.+",
        re.IGNORECASE,
    )),
    ("instagram",  re.compile(
        r"(https?://)?(www\.)?instagram\.com/(p|reel|tv|stories)/.+",
        re.IGNORECASE,
    )),
    ("facebook",   re.compile(
        r"(https?://)?(www\.|m\.|web\.)?(facebook\.com|fb\.watch)/.+",
        re.IGNORECASE,
    )),
    ("pinterest",  re.compile(
        r"(https?://)?(www\.|[a-z]{2}\.)?pinterest\.(com|ca|co\.uk|fr|de|es|it|jp|nz|ru|se|com\.au|com\.mx|com\.br)/.+",
        re.IGNORECASE,
    )),
    ("tiktok",     re.compile(
        r"(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/.+",
        re.IGNORECASE,
    )),
    ("twitter",    re.compile(
        r"(https?://)?(www\.)?(twitter\.com|x\.com|t\.co)/.+",
        re.IGNORECASE,
    )),
    ("reddit",     re.compile(
        r"(https?://)?(www\.|old\.|new\.)?reddit\.com/.+|https?://v\.redd\.it/.+",
        re.IGNORECASE,
    )),
]

def detect_platform(url: str) -> str:
    """Return platform name string for a URL, or 'generic'."""
    for name, pattern in _PLATFORM_PATTERNS:
        if pattern.match(url.strip()):
            return name
    return "generic"

def is_supported_url(url: str) -> bool:
    """True if URL starts with http/https (basic sanity check)."""
    return bool(re.match(r"https?://", url.strip(), re.IGNORECASE))


# ── Shared base options ───────────────────────────────────────────────────────

def _base_opts() -> dict:
    """Options shared across all platforms."""
    opts: dict = {
        "quiet":            True,
        "no_warnings":      True,
        "noplaylist":       True,
        "outtmpl":          str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "writeinfojson":    False,
        "writedescription": False,
        "writethumbnail":   False,
        "embedthumbnail":   False,
        "retries":          10,
        "fragment_retries": 10,
        "extractor_retries":5,
        "file_access_retries": 5,
        "socket_timeout":   30,
        "http_headers": {
            "User-Agent": random.choice(USER_AGENTS),
        },
    }
    if YTDL_PROXY:
        opts["proxy"] = YTDL_PROXY
        logger.debug("Using proxy: %s", YTDL_PROXY)
    return opts


# ── Per-platform option builders ──────────────────────────────────────────────

def ydl_opts_youtube(use_cookies: bool = True) -> dict:
    """
    YouTube options with full bypass stack.
    Client chain: android_vr → tv → tv_downgraded → web
    No PO token / bgutil / Deno required at these clients.
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "format_sort":         ["res", "vcodec:h264", "acodec:m4a", "br"],
        "merge_output_format": "mp4",
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "tv", "tv_downgraded", "web"],
            }
        },
    })
    if use_cookies:
        cs = youtube_cookie_status()
        if cs["ok"]:
            opts["cookiefile"] = COOKIES_FILE
            logger.info("YouTube cookies loaded (%d YT lines)", cs.get("yt_lines", 0))
        else:
            logger.warning("YouTube cookies problem: %s", cs["reason"])
    logger.info("yt-dlp client chain: android_vr + tv + tv_downgraded + web")
    return opts


def ydl_opts_instagram(use_cookies: bool = True) -> dict:
    """
    Instagram options.
    Public Reels work without cookies. Stories/private content require cookies.
    Set IG_COOKIES or FB_COOKIES env var for authenticated downloads.
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        # Instagram serves HLS and DASH — prefer MP4 direct streams when available
        "format_sort":         ["res", "ext:mp4", "br"],
    })
    if use_cookies:
        cookie_file = best_cookie_file_for("instagram")
        if cookie_file and Path(cookie_file).exists():
            opts["cookiefile"] = cookie_file
            logger.info("Instagram cookies loaded from %s", cookie_file)
        else:
            logger.info("No Instagram cookies — public content only")
    return opts


def ydl_opts_facebook(use_cookies: bool = True) -> dict:
    """
    Facebook options.
    Public videos work without cookies. FB Watch / private groups require cookies.
    Set FB_COOKIES env var for authenticated downloads.
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "format_sort":         ["res", "ext:mp4", "br"],
    })
    if use_cookies:
        cookie_file = best_cookie_file_for("facebook")
        if cookie_file and Path(cookie_file).exists():
            opts["cookiefile"] = cookie_file
            logger.info("Facebook cookies loaded from %s", cookie_file)
        else:
            logger.info("No Facebook cookies — public content only")
    return opts


def ydl_opts_pinterest() -> dict:
    """
    Pinterest options.
    Pinterest video pins are public — no cookies needed.
    yt-dlp extracts the MP4 URL directly from the pin page.
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    })
    return opts


def ydl_opts_tiktok() -> dict:
    """
    TikTok options.
    Public videos work without cookies. Uses a mobile UA for best compatibility.
    """
    opts = _base_opts()
    opts["http_headers"]["User-Agent"] = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
        "Mobile/15E148 Safari/604.1"
    )
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        # TikTok watermark: prefer non-watermarked (download_addr) if available
        "extractor_args": {
            "tiktok": {"webpage_download": ["1"]},
        },
    })
    return opts


def ydl_opts_twitter() -> dict:
    """
    Twitter / X options.
    Public tweets work without cookies. DM videos require auth (unsupported).
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "format_sort":         ["res", "ext:mp4", "br"],
    })
    return opts


def ydl_opts_reddit() -> dict:
    """
    Reddit options.
    v.redd.it videos are public. Reddit merges video+audio automatically.
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    })
    return opts


def ydl_opts_generic() -> dict:
    """
    Generic fallback for any yt-dlp supported site not listed above.
    (Vimeo, Dailymotion, Twitch clips, etc.)
    """
    opts = _base_opts()
    opts.update({
        "format":              "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "format_sort":         ["res", "ext:mp4", "br"],
    })
    return opts


# ── Unified entry point ───────────────────────────────────────────────────────

def ydl_opts_for(url: str) -> dict:
    """Return the correct yt-dlp options dict for a given URL."""
    platform = detect_platform(url)
    logger.info("Platform detected: %s for %s", platform, url[:60])
    return {
        "youtube":   ydl_opts_youtube,
        "instagram": ydl_opts_instagram,
        "facebook":  ydl_opts_facebook,
        "pinterest": ydl_opts_pinterest,
        "tiktok":    ydl_opts_tiktok,
        "twitter":   ydl_opts_twitter,
        "reddit":    ydl_opts_reddit,
        "generic":   ydl_opts_generic,
    }[platform]()


# ── Platform display info (for UI) ────────────────────────────────────────────

PLATFORM_EMOJI: dict[str, str] = {
    "youtube":   "▶️",
    "instagram": "📸",
    "facebook":  "👥",
    "pinterest": "📌",
    "tiktok":    "🎵",
    "twitter":   "🐦",
    "reddit":    "🟠",
    "generic":   "🌐",
}

def platform_label(url: str) -> str:
    p = detect_platform(url)
    emoji = PLATFORM_EMOJI.get(p, "🌐")
    return f"{emoji} {p.capitalize()}"
