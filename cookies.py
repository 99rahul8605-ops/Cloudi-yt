"""
cookies.py — Cookie file management for YouTube, Instagram, and Facebook.

Env vars (set on Render / Railway / Docker):
  YOUTUBE_COOKIES   — full contents of a YouTube cookies.txt (Netscape format)
  FB_COOKIES        — full contents of Facebook/Instagram cookies.txt
  IG_COOKIES        — Instagram-only cookies.txt (takes priority over FB_COOKIES for IG)

All three are written to disk on startup so they survive ephemeral filesystems.
"""

import logging
from pathlib import Path

from config import COOKIES_FILE, FB_COOKIES_FILE, IG_COOKIES_FILE
import os

logger = logging.getLogger(__name__)


# ── Startup initialisation ────────────────────────────────────────────────────

def init_cookies_from_env() -> None:
    """Write cookie files from environment variables on startup."""
    _write_cookie_env("YOUTUBE_COOKIES", COOKIES_FILE, "youtube.com")
    _write_cookie_env("FB_COOKIES",      FB_COOKIES_FILE, "facebook.com")
    _write_cookie_env("IG_COOKIES",      IG_COOKIES_FILE, "instagram.com")


def _write_cookie_env(env_key: str, filepath: str, domain_hint: str) -> None:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return
    try:
        Path(filepath).write_text(raw, encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        relevant = [l for l in lines if domain_hint.split(".")[0] in l]
        logger.info(
            "✅ %s written from %s env var (%d total lines, %d %s lines)",
            filepath, env_key, len(lines), len(relevant), domain_hint,
        )
    except Exception as e:
        logger.error("❌ Failed to write %s from %s: %s", filepath, env_key, e)


# ── Cookie status checks ──────────────────────────────────────────────────────

def cookie_status(filepath: str = COOKIES_FILE,
                  domain_keywords: list[str] | None = None) -> dict:
    """Return detailed status of a cookie file."""
    if domain_keywords is None:
        domain_keywords = ["youtube.com", "google.com"]

    path = Path(filepath)
    if not path.exists():
        return {"ok": False, "reason": "File not found", "path": str(path.resolve())}
    size = path.stat().st_size
    if size < 100:
        return {"ok": False,
                "reason": f"File too small ({size} bytes) – probably empty/placeholder",
                "path": str(path.resolve()), "size": size}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception as e:
        return {"ok": False, "reason": f"Cannot read file: {e}", "path": str(path.resolve())}

    real_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    yt_lines   = [l for l in real_lines if any(kw in l for kw in domain_keywords)]

    if not real_lines:
        return {"ok": False, "reason": "File has no cookie data (only comments/blank lines)",
                "path": str(path.resolve())}
    if not yt_lines:
        return {"ok": False,
                "reason": f"No {'/'.join(domain_keywords)} cookies found",
                "path": str(path.resolve()), "total_lines": len(real_lines)}

    has_sapisid = any("SAPISID" in l for l in yt_lines)
    has_sid     = any("\tSID\t" in l or "\t__Secure-1PSID\t" in l for l in yt_lines)
    sample      = yt_lines[0][:120] if yt_lines else ""

    return {
        "ok":          True,
        "path":        str(path.resolve()),
        "size":        size,
        "total":       len(real_lines),
        "yt_lines":    len(yt_lines),
        "has_sapisid": has_sapisid,
        "has_sid":     has_sid,
        "sample":      sample,
    }


def youtube_cookie_status() -> dict:
    return cookie_status(COOKIES_FILE, ["youtube.com", "google.com"])

def facebook_cookie_status() -> dict:
    return cookie_status(FB_COOKIES_FILE, ["facebook.com", "instagram.com"])

def instagram_cookie_status() -> dict:
    # Prefer dedicated IG cookies, fall back to FB cookies
    cs = cookie_status(IG_COOKIES_FILE, ["instagram.com"])
    if cs["ok"]:
        return cs
    return cookie_status(FB_COOKIES_FILE, ["instagram.com", "facebook.com"])


def best_cookie_file_for(platform: str) -> str | None:
    """Return the best available cookie file path for a given platform, or None."""
    if platform in ("instagram",):
        cs = instagram_cookie_status()
        if cs["ok"]:
            p = Path(IG_COOKIES_FILE)
            return str(p) if p.exists() else FB_COOKIES_FILE
    if platform in ("facebook",):
        cs = facebook_cookie_status()
        if cs["ok"]:
            return FB_COOKIES_FILE
    if platform in ("youtube",):
        cs = youtube_cookie_status()
        if cs["ok"]:
            return COOKIES_FILE
    return None
