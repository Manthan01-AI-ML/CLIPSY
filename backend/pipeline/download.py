"""
backend/pipeline/download.py

Hardened YouTube downloader.

Why this exists in v2:
  YouTube's API has been increasingly aggressive about blocking yt-dlp's default
  format requests. We saw repeated `Requested format is not available` failures
  in production when users submitted real videos. The fix is a *cascade* of
  format strings, each progressively less ambitious — at least one will work
  for any public video on YouTube.

Strategy:
  1. Try preferred format (720p mp4 + best audio, merged)
  2. If that fails, try any combined mp4 stream
  3. If that fails, try `best` (no constraints — yt-dlp picks anything)
  4. Surface a USER-FRIENDLY error if all three fail (not the raw yt-dlp message)

Resilience features:
  - Multiple yt-dlp client extractors tried (web → android → ios)
  - Extended timeouts for slow networks
  - Cookie support for age/region-restricted content
  - Detailed logging at each attempt for easier debugging
  - Maps common errors to actionable messages users can understand
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

import yt_dlp

from backend.services.storage import _user_dir

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when yt-dlp fails (bad URL, private video, geo-blocked, etc.)."""


# Format cascade: try each in order. First success wins.
# Each entry is (label, format_string).
FORMAT_FALLBACKS: list[tuple[str, str]] = [
    # 1. Preferred: 720p mp4 video + m4a audio, merged. Best quality/size tradeoff.
    ("preferred-720p-mp4",
     "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]"),

    # 2. Looser: any pre-combined mp4 (avoids YouTube's split-stream restrictions)
    ("any-combined-mp4", "best[ext=mp4]/best[height<=720]"),

    # 3. Bare minimum: just give us SOMETHING playable. Any container, any quality.
    ("any-format", "best"),
]

# YouTube extractor clients to try. The 'web' client requires a JavaScript
# runtime (which yt-dlp's nightly may need); 'android' and 'ios' don't.
# We list all of them so yt-dlp can fall back internally.
EXTRACTOR_CLIENTS = ["android", "ios", "web", "tv"]


def _build_ydl_opts(
    out_template: str,
    format_str: str,
    cookies_path: str | None = None,
) -> dict:
    """Build yt-dlp options for one attempt."""
    opts: dict = {
        "format": format_str,
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 60,        # was 30; some YT edges are slow
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        # Nudge YouTube extractor: try alternate clients which often unlock
        # formats the default web client can't see.
        "extractor_args": {
            "youtube": {
                "player_client": EXTRACTOR_CLIENTS,
                # Skip dash if web client fails (saves time on retry)
                "skip": ["hls"] if format_str == "best" else [],
            }
        },
        # Add a real-browser User-Agent string. yt-dlp's default UA gets blocked
        # more aggressively than a typical browser fingerprint.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookies_path and Path(cookies_path).exists():
        opts["cookiefile"] = cookies_path
    return opts


def _humanize_youtube_error(raw_err: str) -> str:
    """Convert raw yt-dlp errors into user-friendly messages."""
    s = raw_err.lower()
    if "private" in s:
        return "This video is private — only the channel owner can access it."
    if "members-only" in s or "this video is available to this channel" in s:
        return "This is a members-only video that requires a paid subscription."
    if "premiere" in s or "this live event will begin" in s:
        return "This is an upcoming or scheduled live event, not yet available."
    if "country" in s or "available in your" in s or "not available in your country" in s:
        return "This video is geo-blocked and can't be downloaded from our region."
    if "age" in s and ("restrict" in s or "confirm" in s or "sign in" in s):
        return "This video is age-restricted. Sign-in cookies are required (contact support)."
    if "removed" in s or "no longer available" in s or "deleted" in s:
        return "This video has been removed or deleted from YouTube."
    if "copyright" in s:
        return "This video has been blocked due to a copyright claim."
    if "video unavailable" in s or "this video is unavailable" in s:
        return "This video is unavailable. It may have been deleted, set private, or geo-blocked."
    if "format is not available" in s:
        return ("YouTube changed its video formats — our downloader may need updating. "
                "Try a different video, or contact support.")
    if "sign in to confirm" in s and "bot" in s:
        return ("YouTube is requiring sign-in to download this video (bot check). "
                "Try a different video, or contact support.")
    if "http error 403" in s or "forbidden" in s:
        return "YouTube refused our download request. Try a different video."
    if "connection" in s or "timeout" in s or "timed out" in s:
        return "Network error reaching YouTube. Try again in a moment."
    return f"Download failed. ({raw_err[:120]})"


def _resolve_cookies_path() -> str | None:
    """Find a cookies file if one is configured on disk."""
    # Check well-known locations
    candidates = [
        os.environ.get("YOUTUBE_COOKIES_PATH"),
        "/app/cookies/youtube.txt",
        "/app/cookies.txt",
    ]
    for c in candidates:
        if c and Path(c).exists() and Path(c).stat().st_size > 0:
            return c
    return None


def download_youtube(user_id: uuid.UUID, job_id: uuid.UUID, url: str) -> Path:
    """
    Download a YouTube video to local disk. Returns the saved file path.

    Tries a cascade of format strings until one succeeds. If all fail, raises
    DownloadError with a USER-FRIENDLY message.
    """
    # Light URL sanity check
    if not url or not isinstance(url, str):
        raise DownloadError("Invalid URL")
    if "youtube.com" not in url and "youtu.be" not in url:
        raise DownloadError("Only YouTube URLs are supported right now.")

    out_dir = _user_dir(user_id, "raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / f"{job_id}.%(ext)s")

    cookies_path = _resolve_cookies_path()
    if cookies_path:
        logger.info(f"[{job_id}] using cookies: {cookies_path}")

    last_error: str | None = None

    for attempt_idx, (label, fmt) in enumerate(FORMAT_FALLBACKS, start=1):
        # Clean any partial files from prior failed attempts in this job
        for stale in out_dir.glob(f"{job_id}.*"):
            try:
                stale.unlink()
            except OSError:
                pass

        logger.info(
            f"[{job_id}] downloading (attempt {attempt_idx}/{len(FORMAT_FALLBACKS)}): "
            f"format='{label}'"
        )

        ydl_opts = _build_ydl_opts(out_template, fmt, cookies_path)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            # Locate the resulting file
            candidates = list(out_dir.glob(f"{job_id}.*"))
            # filter out any empty/zero-byte stragglers
            candidates = [c for c in candidates if c.stat().st_size > 0]
            if not candidates:
                last_error = "yt-dlp succeeded but no output file was produced"
                logger.warning(f"[{job_id}] attempt {attempt_idx}: {last_error}")
                continue

            file_path = max(candidates, key=lambda p: p.stat().st_size)
            size_mb = file_path.stat().st_size / (1024 * 1024)

            title = "unknown"
            if info and isinstance(info, dict):
                title = str(info.get("title", "unknown"))[:60]

            logger.info(
                f"[{job_id}] downloaded ({label}): {file_path.name} "
                f"({size_mb:.1f} MB, title='{title}')"
            )
            return file_path

        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
            logger.warning(f"[{job_id}] attempt {attempt_idx} failed: {last_error}")
            continue
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[{job_id}] attempt {attempt_idx} unexpected error: {last_error}")
            continue

    # All attempts exhausted
    msg = _humanize_youtube_error(last_error or "Unknown error")
    raise DownloadError(msg)


def extract_video_meta(file_path: Path) -> dict:
    """Use ffprobe to extract duration + basic info."""
    import subprocess, json

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration:stream=width,height,codec_type",
                "-of", "json",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.warning(f"ffprobe failed on {file_path}: {e}")
        return {}

    duration = float(data.get("format", {}).get("duration", 0))
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        {},
    )
    return {
        "duration_sec": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
    }