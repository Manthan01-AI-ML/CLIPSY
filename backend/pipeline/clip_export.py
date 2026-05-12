"""
backend/pipeline/clip_export.py

Quality-tiered export for finished clips.

Re-encodes a rendered clip to one of three quality tiers (480p, 720p, 1080p)
with the FFmpeg flags from product spec:

  480p:  -vf scale=854:480   -crf 28 -preset fast    (~1.5 Mbps)
  720p:  -vf scale=1280:720  -crf 23 -preset medium  (~3.0 Mbps)
  1080p: -vf scale=1920:1080 -crf 18 -preset slow    (~6.0 Mbps)

Output file is written next to the source clip with `.{quality}.mp4` suffix
(e.g. clip_01.mp4 → clip_01.480p.mp4) so it's both cacheable AND cleanable
when the clip is reset or deleted.

The encoder preserves aspect ratio: scale's `-2` syntax keeps width/height
proportional. So a 9:16 clip stays 9:16 — `1920:1080` becomes `1080:1920`
effectively for portrait videos via aspect-aware scaling.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ExportError(Exception):
    """Raised when an export job fails."""


# Per-quality settings. (label, target_short_edge, crf, preset, est_bitrate_bps)
# `target_short_edge` = the SHORTER dimension of the output. We use ffmpeg's
# scale-with-aspect-preservation so portrait clips don't get stretched.
QUALITY_PROFILES: dict[str, dict] = {
    "480p": {
        "label": "480p",
        "short_edge": 480,
        "crf": 28,
        "preset": "fast",
        "est_bitrate_bps": 1_500_000,   # 1.5 Mbps target
    },
    "720p": {
        "label": "720p",
        "short_edge": 720,
        "crf": 23,
        "preset": "medium",
        "est_bitrate_bps": 3_000_000,   # 3.0 Mbps target
    },
    "1080p": {
        "label": "1080p",
        "short_edge": 1080,
        "crf": 18,
        "preset": "slow",
        "est_bitrate_bps": 6_000_000,   # 6.0 Mbps target
    },
}


def get_clip_duration(clip_path: Path) -> float:
    """Probe the clip duration in seconds. Returns 0.0 on failure (caller's choice)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(clip_path)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError, ValueError, KeyError):
        return 0.0


def estimate_export_size_bytes(clip_path: Path, quality: str) -> int:
    """
    Estimate encoded file size in bytes for a given quality.

    File size ≈ duration × bitrate ÷ 8. Uses target bitrates from QUALITY_PROFILES.
    Returns 0 if duration probe fails (UI should hide the estimate then).
    """
    if quality not in QUALITY_PROFILES:
        return 0
    duration = get_clip_duration(clip_path)
    if duration <= 0:
        return 0
    bitrate = QUALITY_PROFILES[quality]["est_bitrate_bps"]
    # +5% audio overhead estimate (AAC 128k ≈ 16 KB/s on top of video)
    audio_overhead = duration * 16_000  # bytes
    return int((duration * bitrate / 8) + audio_overhead)


def output_path_for(clip_path: Path, quality: str) -> Path:
    """The deterministic location for an exported file."""
    return clip_path.with_suffix(f".{quality}.mp4")


def export_clip(
    clip_path: Path,
    quality: str,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> Path:
    """
    Re-encode the clip to the target quality. Writes to `clip_path.{quality}.mp4`
    via a temp file + atomic move so partial outputs don't surface to users.

    Args:
        clip_path: source clip (already rendered, may include canvas/outro)
        quality: one of "480p" | "720p" | "1080p"
        progress_callback: optional fn(int 0-100) called as encoding progresses

    Returns:
        Path to the encoded file on success.

    Raises:
        ExportError on any failure.
    """
    if quality not in QUALITY_PROFILES:
        raise ExportError(f"Unknown quality: {quality}")
    if not clip_path.exists():
        raise ExportError(f"Source clip missing: {clip_path}")

    profile = QUALITY_PROFILES[quality]
    target_path = output_path_for(clip_path, quality)
    temp_path = target_path.with_suffix(f".{quality}.tmp.mp4")

    # Cache check: if a fresh export already exists, reuse it
    if target_path.exists():
        src_mtime = clip_path.stat().st_mtime
        out_mtime = target_path.stat().st_mtime
        if out_mtime >= src_mtime and target_path.stat().st_size > 0:
            logger.info(f"[export] cache hit: {target_path.name}")
            if progress_callback:
                progress_callback(100)
            return target_path

    duration = get_clip_duration(clip_path)
    short_edge = profile["short_edge"]

    # Aspect-preserving scale.
    # If source is portrait (h > w), we scale by height: scale=-2:short_edge
    # If source is landscape (w >= h), we scale by width: scale=short_edge:-2
    # ffprobe to find orientation:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(clip_path)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        info = json.loads(probe.stdout).get("streams", [{}])[0]
        sw = int(info.get("width") or 0)
        sh = int(info.get("height") or 0)
    except Exception as e:
        raise ExportError(f"could not probe source dimensions: {e}")

    if sw <= 0 or sh <= 0:
        raise ExportError("source has zero/invalid dimensions")

    # For portrait (typical Reels/Shorts): width is the short edge.
    # For landscape: height is the short edge.
    # We scale the SHORT edge to the target (e.g. 720), and let the long edge
    # auto-compute via -2 (must be even for x264).
    if sh >= sw:
        # portrait
        vf_scale = f"scale=-2:{short_edge}" if sw < short_edge else f"scale={short_edge}:-2"
        # Wait — for portrait, short edge = width. So scale width to short_edge.
        vf_scale = f"scale={short_edge}:-2"
    else:
        # landscape: short edge = height
        vf_scale = f"scale=-2:{short_edge}"

    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-i", str(clip_path),
        "-vf", vf_scale,
        "-c:v", "libx264",
        "-crf", str(profile["crf"]),
        "-preset", profile["preset"],
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        # Progress lines on stderr we can parse
        "-progress", "pipe:2",
        "-loglevel", "error",
        str(temp_path),
    ]

    logger.info(
        f"[export] {clip_path.name} → {quality} "
        f"(crf={profile['crf']}, preset={profile['preset']}, dur={duration:.1f}s)"
    )

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    # Stream stderr line by line and parse `out_time_ms=` + `progress=` markers.
    # ffmpeg writes "out_time_ms=12345678\nprogress=continue\n" or similar.
    last_pct = 0
    last_callback_time = 0.0
    if proc.stderr is not None:
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            m = re.match(r"out_time_ms=(\d+)", line)
            if m and duration > 0:
                cur_sec = int(m.group(1)) / 1_000_000
                pct = max(0, min(99, int(cur_sec * 100 / duration)))
                # Throttle callbacks: only fire when pct changes by ≥1 AND
                # at most every 250ms
                now = time.time()
                if pct > last_pct and (now - last_callback_time) > 0.25:
                    last_pct = pct
                    last_callback_time = now
                    if progress_callback:
                        try:
                            progress_callback(pct)
                        except Exception:
                            pass
            elif line == "progress=end":
                break

    rc = proc.wait()
    if rc != 0:
        # Drain any remaining stderr for the error message
        err_tail = ""
        if proc.stderr:
            err_tail = (proc.stderr.read() or "")[-300:]
        temp_path.unlink(missing_ok=True)
        raise ExportError(f"ffmpeg failed (rc={rc}): {err_tail}")

    if not temp_path.exists() or temp_path.stat().st_size == 0:
        temp_path.unlink(missing_ok=True)
        raise ExportError("ffmpeg produced empty output")

    # Atomic replace
    import shutil
    shutil.move(str(temp_path), str(target_path))

    if progress_callback:
        try:
            progress_callback(100)
        except Exception:
            pass

    final_size_mb = target_path.stat().st_size / (1024 * 1024)
    logger.info(f"[export] complete: {target_path.name} ({final_size_mb:.1f} MB)")
    return target_path


def cleanup_exports(clip_path: Path) -> int:
    """Remove all exported derivatives of a clip. Returns count removed."""
    parent = clip_path.parent
    base = clip_path.stem  # 'clip_01'
    count = 0
    for q in QUALITY_PROFILES:
        out = parent / f"{base}.{q}.mp4"
        if out.exists():
            out.unlink(missing_ok=True)
            count += 1
        tmp = parent / f"{base}.{q}.tmp.mp4"
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return count