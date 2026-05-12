"""
backend/pipeline/silence.py

Session C: Silence detection + removal for tighter clips.

Architecture:
  1. Extract the clip segment (start→end from source)
  2. Run ffmpeg silencedetect on it → parse timestamps
  3. Build an inverted 'select' filter that keeps only non-silent parts
  4. Re-encode with concatenated non-silent segments

Research-backed parameters:
  - Threshold: -30 dB (standard for speech)
  - Min silence duration: 0.8s (pauses shorter than this are natural speech rhythm)
  - Keep 0.15s buffer at start/end of each silence (don't cut mid-word)

This is the "jump cut" technique used by top creators — it doesn't cut EVERY
pause, just the long dead air that kills pacing.
"""
from __future__ import annotations

import logging
import re
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


# Silence detection parameters
SILENCE_THRESHOLD_DB = -30     # dB below which audio is considered "silent"
MIN_SILENCE_DURATION = 0.8     # seconds — shorter pauses are natural speech rhythm
SILENCE_BUFFER = 0.15          # seconds to keep at start/end of each silence (prevents mid-word cuts)

# Safety limits
MAX_SILENCE_REMOVAL_RATIO = 0.40    # don't remove more than 40% of the clip
MIN_KEEP_SEGMENT_DURATION = 0.3     # don't produce micro-fragments


class SilenceRemovalError(Exception):
    pass


def _detect_silences(video_file: Path, job_id: uuid.UUID) -> list[tuple[float, float]]:
    """
    Run ffmpeg silencedetect, return list of (silence_start, silence_end) tuples.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(video_file),
        "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={MIN_SILENCE_DURATION}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, timeout=180, text=True)
    except subprocess.TimeoutExpired:
        raise SilenceRemovalError("silence detection timeout (>3min)")

    # silencedetect writes to stderr
    stderr = result.stderr

    silences: list[tuple[float, float]] = []
    # Parse patterns:
    #   [silencedetect @ 0x...] silence_start: 1.234
    #   [silencedetect @ 0x...] silence_end: 2.345 | silence_duration: 1.111
    pending_start: float | None = None
    for line in stderr.splitlines():
        m_start = re.search(r"silence_start:\s*([\-\d.]+)", line)
        if m_start:
            try:
                pending_start = float(m_start.group(1))
            except ValueError:
                pending_start = None
            continue
        m_end = re.search(r"silence_end:\s*([\-\d.]+)", line)
        if m_end and pending_start is not None:
            try:
                silence_end = float(m_end.group(1))
                if silence_end > pending_start:
                    silences.append((pending_start, silence_end))
            except ValueError:
                pass
            pending_start = None

    logger.info(
        f"[{job_id}] silence detection: found {len(silences)} silent regions "
        f"(threshold={SILENCE_THRESHOLD_DB}dB, min={MIN_SILENCE_DURATION}s)"
    )
    return silences


def _get_duration(video_file: Path) -> float:
    """Get video duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_file)],
            check=True, capture_output=True, timeout=30, text=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        raise SilenceRemovalError(f"Could not get duration: {e}") from e


def _compute_keep_segments(
    silences: list[tuple[float, float]], total_duration: float
) -> list[tuple[float, float]]:
    """
    Given silent regions and total duration, return the keep-segments:
    the non-silent parts we want to include in output.
    Applies SILENCE_BUFFER to prevent mid-word cuts.
    """
    # Shrink silences by buffer on each side (keep speech)
    adjusted = []
    for s, e in silences:
        adjusted_start = s + SILENCE_BUFFER
        adjusted_end = e - SILENCE_BUFFER
        if adjusted_end > adjusted_start:
            adjusted.append((adjusted_start, adjusted_end))

    # Build keep segments = complement of silences
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in adjusted:
        if s > cursor:
            keeps.append((cursor, s))
        cursor = e
    if cursor < total_duration:
        keeps.append((cursor, total_duration))

    # Filter out micro-fragments (< MIN_KEEP_SEGMENT_DURATION)
    keeps = [(s, e) for s, e in keeps if e - s >= MIN_KEEP_SEGMENT_DURATION]

    return keeps


def remove_silences(
    input_video: Path,
    output_video: Path,
    job_id: uuid.UUID,
) -> dict:
    """
    Remove silent regions from a video, producing a tighter version.

    Returns a dict with:
      {
        "silences_found": int,     # how many silent regions detected
        "seconds_removed": float,  # total seconds cut
        "original_duration": float,
        "new_duration": float,
        "reduction_pct": int,      # % of original length removed
        "skipped": bool,           # True if we decided NOT to remove (safety)
        "reason": str,             # human-readable reason if skipped
      }

    Side effect: writes to output_video (or copies input to output if skipped).
    """
    original_duration = _get_duration(input_video)
    silences = _detect_silences(input_video, job_id)

    if not silences:
        # No silences found — just copy input to output
        logger.info(f"[{job_id}] no silences found, copying unchanged")
        _copy_video(input_video, output_video)
        return {
            "silences_found": 0,
            "seconds_removed": 0.0,
            "original_duration": original_duration,
            "new_duration": original_duration,
            "reduction_pct": 0,
            "skipped": True,
            "reason": "no silences detected",
        }

    keep_segments = _compute_keep_segments(silences, original_duration)

    if not keep_segments:
        logger.warning(f"[{job_id}] silence removal would leave 0 segments — skipping")
        _copy_video(input_video, output_video)
        return {
            "silences_found": len(silences),
            "seconds_removed": 0.0,
            "original_duration": original_duration,
            "new_duration": original_duration,
            "reduction_pct": 0,
            "skipped": True,
            "reason": "all audio was silent",
        }

    # Compute how much we'd remove
    kept_duration = sum(e - s for s, e in keep_segments)
    removed_seconds = original_duration - kept_duration
    removal_ratio = removed_seconds / original_duration if original_duration > 0 else 0

    # Safety check: if removing too much, skip
    if removal_ratio > MAX_SILENCE_REMOVAL_RATIO:
        logger.warning(
            f"[{job_id}] silence removal would cut {removal_ratio*100:.0f}% "
            f"(max {MAX_SILENCE_REMOVAL_RATIO*100:.0f}%) — skipping"
        )
        _copy_video(input_video, output_video)
        return {
            "silences_found": len(silences),
            "seconds_removed": 0.0,
            "original_duration": original_duration,
            "new_duration": original_duration,
            "reduction_pct": 0,
            "skipped": True,
            "reason": f"would remove {removal_ratio*100:.0f}% (safety cap {MAX_SILENCE_REMOVAL_RATIO*100:.0f}%)",
        }

    # Build select filter: keeps only segments in keep_segments
    # between(t,A,B) returns 1 if A <= t < B
    select_expr_parts = [f"between(t,{s:.3f},{e:.3f})" for s, e in keep_segments]
    select_expr = "+".join(select_expr_parts)

    # For video: select frames, then reset PTS
    # For audio: aselect same expression, then reset PTS
    vf = f"select='{select_expr}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{select_expr}',asetpts=N/SR/TB"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_video),
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-loglevel", "error",
        str(output_video),
    ]

    logger.info(
        f"[{job_id}] removing silences: {len(silences)} regions, "
        f"{removed_seconds:.1f}s cut ({removal_ratio*100:.0f}%), "
        f"{len(keep_segments)} keep segments"
    )

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError as e:
        logger.error(
            f"[{job_id}] silence removal ffmpeg failed: {e.stderr.decode()[:500]}"
        )
        # Fall back: copy original unchanged
        _copy_video(input_video, output_video)
        return {
            "silences_found": len(silences),
            "seconds_removed": 0.0,
            "original_duration": original_duration,
            "new_duration": original_duration,
            "reduction_pct": 0,
            "skipped": True,
            "reason": f"ffmpeg error: {e.stderr.decode()[:100]}",
        }
    except subprocess.TimeoutExpired:
        logger.error(f"[{job_id}] silence removal timeout")
        _copy_video(input_video, output_video)
        return {
            "silences_found": len(silences),
            "seconds_removed": 0.0,
            "original_duration": original_duration,
            "new_duration": original_duration,
            "reduction_pct": 0,
            "skipped": True,
            "reason": "ffmpeg timeout",
        }

    new_duration = _get_duration(output_video)
    logger.info(
        f"[{job_id}] ✓ silence removed: {original_duration:.1f}s → {new_duration:.1f}s "
        f"({int(removal_ratio*100)}% tighter)"
    )
    return {
        "silences_found": len(silences),
        "seconds_removed": removed_seconds,
        "original_duration": original_duration,
        "new_duration": new_duration,
        "reduction_pct": int(removal_ratio * 100),
        "skipped": False,
        "reason": "",
    }


def _copy_video(src: Path, dst: Path) -> None:
    """Safe copy — uses ffmpeg so we handle same source-dest cleanly."""
    if src.resolve() == dst.resolve():
        return
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-c", "copy", "-loglevel", "error", str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Last resort: shutil copy
        import shutil
        shutil.copyfile(src, dst)
