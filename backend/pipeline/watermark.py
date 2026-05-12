"""
backend/pipeline/watermark.py

Adds a small, professional Clipsy watermark to free-tier clip outputs.

Design:
  - Bottom-right corner placement (Gemini/Loom-style — out of the way)
  - Small (~4% of frame width) — visible but not intrusive
  - Semi-transparent (~75% opacity) so it doesn't dominate
  - Faint shadow behind so it stays readable on any background
  - Uses an SVG-rendered PNG burned in via ffmpeg overlay (no font dependency)

Anti-abuse strategy:
  This module just RENDERS the watermark. The DECISION of whether to apply
  it lives in `should_apply_watermark()` — kept here so policy is in one place.

  Default policy: free-tier accounts ALWAYS get the watermark. This is
  predictable (users know what they're getting) and professional (Loom, Canva,
  Figma all do this). Burst-based abuse is mitigated separately by IP rate
  limiting.

  Configurable via env: FREE_TIER_WATERMARK_THRESHOLD
    - 0 (default): watermark every free clip from #1
    - N: only watermark from the (N+1)th lifetime clip onwards.
         Lets you offer "first N clips free without watermark" if you prefer
         that strategy.
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class WatermarkError(Exception):
    pass


# --- Policy gating -----------------------------------------------------------

def should_apply_watermark(
    user_plan: str,
    user_clips_lifetime: int,
    clip_index_in_job: int = 0,
) -> bool:
    """
    Decide whether to apply a watermark to this clip.

    Args:
        user_plan: "free" | "starter" | "pro" | "agency"
        user_clips_lifetime: how many clips this user has EVER generated
                             (used as anti-abuse signal — never resets)
        clip_index_in_job: 0-based index of THIS clip within the current job
                           (clip #1 in the batch = 0, clip #2 = 1, etc.)

    Returns:
        True if watermark should be added.

    Policy (default; override via env vars):
        Paid plans: never watermark.
        Free plan, current job:
          - The first FREE_CLIPS_PER_JOB clips of EVERY job are clean
            (default 1 — user always gets a "premium tease" each time)
          - All other clips in the same job get watermarked
        Free plan, lifetime safety:
          - Beyond FREE_TIER_LIFETIME_LIMIT lifetime clips, EVERY clip is
            watermarked (default 1000 = effectively unlimited free preview).
            This is a backstop in case someone tries to abuse the per-job
            rule across many accounts; it doesn't kick in for normal users.

    Env-tunable:
        FREE_CLIPS_PER_JOB=2        → first 2 clips per job are clean
        FREE_TIER_LIFETIME_LIMIT=50 → after 50 lifetime clips, all watermarked
    """
    if user_plan and user_plan != "free":
        return False

    # Per-job: every job gives N free clips (the "premium tease")
    free_per_job = int(os.environ.get("FREE_CLIPS_PER_JOB", "1"))
    if clip_index_in_job < free_per_job:
        return False  # this clip is one of the free ones

    # Lifetime safety: prevent extreme abuse (default backstop = 1000 clips)
    lifetime_limit = int(os.environ.get("FREE_TIER_LIFETIME_LIMIT", "1000"))
    if user_clips_lifetime >= lifetime_limit:
        return True

    return True  # default: watermark all non-free clips in the batch


# --- Watermark image generation ---------------------------------------------

# We generate the watermark PNG once at startup and cache it. The PNG is small
# (~4-8 KB), and its dimensions are sized to the OUTPUT clip width, not source.
# Keeping a single asset means consistent appearance across all renders.

_WATERMARK_CACHE: dict[tuple[int, int], Path] = {}


def _get_watermark_png(target_width: int, target_height: int) -> Path:
    """
    Return path to a PNG watermark sized for the given target frame dimensions.
    Cached so we don't regenerate per-clip.

    Implementation: pure-ffmpeg, no rsvg, no SVG. Renders text on a translucent
    rounded-rect-ish background using ffmpeg's color + drawtext + format filters.
    """
    cache_key = (target_width, target_height)
    cached = _WATERMARK_CACHE.get(cache_key)
    if cached and cached.exists():
        return cached

    # Watermark is sized relative to frame: ~4% of width, height auto.
    # On a 1080x1920 frame: ~265px wide, ~44px tall — small but readable.
    wm_w = max(180, target_width // 5)        # 216 on 1080-wide
    wm_h = max(36, wm_w // 6)                  # ~36-44 px tall (6:1 ratio)
    fontsize = max(16, int(wm_h * 0.42))      # ~15-18 px text

    wm_path = Path(f"/tmp/clipsy_watermark_{wm_w}x{wm_h}.png")

    # Pick a font that exists on the container
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]
    fontfile = next((f for f in font_candidates if Path(f).exists()), None)
    if not fontfile:
        raise WatermarkError("No bold font available for watermark text")

    # ffmpeg-only build:
    #   1. Generate translucent black canvas (semi-transparent pill background)
    #   2. drawtext "Clipsy.ai" centered
    # The ROUNDED look comes from the text just sitting on a pill-shaped
    # background; for simplicity we use a plain translucent rectangle.
    # Looks clean and professional at this size.
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black@0.55:s={wm_w}x{wm_h}:d=1,format=rgba",
        "-vf", (
            f"drawtext=text='Clipsy.ai':"
            f"fontfile='{fontfile}':"
            f"fontcolor=white:"
            f"fontsize={fontsize}:"
            f"x=(w-text_w)/2:"
            f"y=(h-text_h)/2"
        ),
        "-frames:v", "1",
        "-loglevel", "error",
        str(wm_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = e.stderr.decode() if hasattr(e, "stderr") and e.stderr else ""
        raise WatermarkError(
            f"Could not generate watermark PNG: {stderr[-300:]}"
        ) from e

    if not wm_path.exists() or wm_path.stat().st_size == 0:
        raise WatermarkError("Watermark PNG was not produced or is empty")

    _WATERMARK_CACHE[cache_key] = wm_path
    return wm_path


# --- Public API: apply watermark to a rendered clip --------------------------

def apply_watermark(
    input_clip: Path,
    output_clip: Path,
    *,
    job_id: uuid.UUID | str,
    rank: int = 0,
) -> dict:
    """
    Burn the Clipsy watermark into a rendered clip.

    Args:
        input_clip: existing rendered MP4 (any aspect ratio)
        output_clip: where to write the watermarked MP4 (atomic write — temp + rename)
        job_id, rank: for logging

    Returns:
        {"applied": True, "watermark_size": "240x40", ...}

    Raises WatermarkError on failure.

    Implementation notes:
      - Probes input dimensions so the watermark scales correctly per aspect
      - Position: bottom-right, 24px margin on both axes
      - Re-encodes video with H.264 CRF 20 (visually lossless), keeps audio as-is
    """
    if not input_clip.exists():
        raise WatermarkError(f"Input clip missing: {input_clip}")

    # Probe input dimensions so the watermark scales correctly
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(input_clip)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        w_str, h_str = probe.stdout.strip().split("x")
        in_w, in_h = int(w_str), int(h_str)
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired) as e:
        raise WatermarkError(f"Could not probe input dimensions: {e}") from e

    wm_png = _get_watermark_png(in_w, in_h)

    # Position: bottom-right with 24px margin
    margin = max(16, in_w // 64)  # ~17px on a 1080-wide frame; scales gracefully

    # Build ffmpeg cmd:
    #   [0:v] = input video stream
    #   [1:v] = watermark PNG (looped to match video duration via -loop 1)
    #   overlay = composite watermark on top, positioned bottom-right
    temp_out = output_clip.with_suffix(".wm_tmp.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_clip),
        "-i", str(wm_png),
        "-filter_complex",
        f"[0:v][1:v]overlay=W-w-{margin}:H-h-{margin}:format=auto,format=yuv420p[out]",
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",         # visually lossless; matches our other quality settings
        "-c:a", "copy",       # no audio re-encode — preserves quality + saves time
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(temp_out),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError as e:
        temp_out.unlink(missing_ok=True)
        stderr = e.stderr.decode()[-500:] if e.stderr else ""
        raise WatermarkError(f"ffmpeg watermark failed: {stderr}") from e
    except subprocess.TimeoutExpired:
        temp_out.unlink(missing_ok=True)
        raise WatermarkError("Watermark render timed out")

    # Atomic replace
    try:
        import shutil
        shutil.move(str(temp_out), str(output_clip))
    except OSError as e:
        temp_out.unlink(missing_ok=True)
        raise WatermarkError(f"Could not move temp output: {e}") from e

    wm_size = wm_png.stat().st_size
    logger.info(
        f"[{job_id}] watermark applied to clip #{rank}: "
        f"{in_w}x{in_h} video, {wm_size}B watermark"
    )

    return {
        "applied": True,
        "input_dims": f"{in_w}x{in_h}",
        "watermark_bytes": wm_size,
        "position": "bottom-right",
        "margin_px": margin,
    }