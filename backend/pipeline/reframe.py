"""
backend/pipeline/reframe.py — Per-segment keyframe reframe support.

User can specify multiple crop centers along the clip timeline:
  - Speaker A from 0-12s
  - Speaker B from 12-25s
  - Speaker A from 25-end

Two transition modes between keyframes:
  - "smooth" — pan from previous to next over `transition_dur` seconds
  - "cut" — instant switch at keyframe time
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Keyframe normalization (handles legacy v1 + new v2 formats)
# =============================================================================
def normalize_user_crop(user_crop: dict | None) -> dict | None:
    """
    Accept either:
      - v1 (legacy): {x_pct, y_pct, zoom}
      - v2 (keyframes): {version: 2, keyframes: [...], transition, transition_dur, zoom}
    Returns canonical v2 form, or None if invalid.
    """
    if not user_crop or not isinstance(user_crop, dict):
        return None

    if user_crop.get("version") == 2 and "keyframes" in user_crop:
        kfs = user_crop.get("keyframes") or []
        if not kfs:
            return None
        clean = []
        for kf in kfs:
            try:
                entry = {
                    "t": float(kf["t"]),
                    "x_pct": float(kf["x_pct"]),
                    "y_pct": float(kf.get("y_pct", 0.5)),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if "transition_dur_in" in kf:
                try:
                    entry["transition_dur_in"] = float(kf["transition_dur_in"])
                except (TypeError, ValueError):
                    pass
            clean.append(entry)
        if not clean:
            return None
        clean.sort(key=lambda k: k["t"])
        return {
            "version": 2,
            "keyframes": clean,
            "zoom": float(user_crop.get("zoom", 1.0)),
            "transition": user_crop.get("transition", "smooth"),
            "transition_dur": float(user_crop.get("transition_dur", 0.4)),
        }

    # v1 → wrap as single keyframe
    try:
        return {
            "version": 2,
            "keyframes": [{
                "t": 0.0,
                "x_pct": float(user_crop.get("x_pct", 0.5)),
                "y_pct": float(user_crop.get("y_pct", 0.5)),
            }],
            "zoom": float(user_crop.get("zoom", 1.0)),
            "transition": "cut",
            "transition_dur": 0.0,
        }
    except (TypeError, ValueError):
        return None


# =============================================================================
# Crop window dimensions (shared across all keyframes — same zoom)
# =============================================================================
def compute_crop_dims(
    source_w: int, source_h: int,
    out_w: int, out_h: int,
    zoom: float = 1.0,
) -> tuple[int, int]:
    """Return (crop_w, crop_h) in source pixels. Even-aligned for libx264."""
    target_aspect = out_w / out_h
    source_aspect = source_w / source_h
    if source_aspect >= target_aspect:
        crop_h_unzoomed = source_h
        crop_w_unzoomed = crop_h_unzoomed * target_aspect
    else:
        crop_w_unzoomed = source_w
        crop_h_unzoomed = crop_w_unzoomed / target_aspect
    zoom = max(1.0, min(4.0, float(zoom)))
    crop_w = int(crop_w_unzoomed / zoom) // 2 * 2
    crop_h = int(crop_h_unzoomed / zoom) // 2 * 2
    return crop_w, crop_h


# =============================================================================
# Build piecewise X(t) / Y(t) FFmpeg expressions from keyframes
# =============================================================================
def _build_piecewise_expr(
    keyframes: list[dict],
    axis: str,
    source_dim: int,
    crop_dim: int,
    transition: str,
    transition_dur: float,
) -> str:
    """
    Build a piecewise FFmpeg expression for crop top-left along one axis.
    `t` in the expression is the clip-relative timestamp (FFmpeg's `t`).

    NOTE: Commas inside the expression must be escaped as \\, because the
    filter parser uses commas to separate filters.
    """
    half = crop_dim / 2

    def to_topleft(pct: float) -> float:
        center = pct * source_dim
        topleft = center - half
        return max(0.0, min(source_dim - crop_dim, topleft))

    pct_key = "x_pct" if axis == "x" else "y_pct"
    pts = [(float(kf["t"]), to_topleft(float(kf[pct_key])))
           for kf in keyframes]
    pts.sort(key=lambda p: p[0])

    if len(pts) == 1:
        return f"{pts[0][1]:.2f}"

    if transition == "cut" or transition_dur <= 0:
        # X(t) = if(lt(t,t1), x0, if(lt(t,t2), x1, ..., x_n))
        expr = f"{pts[-1][1]:.2f}"
        for i in range(len(pts) - 2, -1, -1):
            t_next = pts[i + 1][0]
            x_i = pts[i][1]
            expr = f"if(lt(t\\,{t_next:.4f})\\,{x_i:.2f}\\,{expr})"
        return expr

    # Smooth: cubic ease-in-out pan over seg_dur seconds, centered on each boundary.
    expr = f"{pts[-1][1]:.2f}"
    for i in range(len(pts) - 2, -1, -1):
        t_curr, x_curr = pts[i]
        t_next, x_next = pts[i + 1]
        seg_dur = float(keyframes[i + 1].get("transition_dur_in") or transition_dur)
        half_dur = seg_dur / 2.0
        b_start = max(t_curr, t_next - half_dur)
        b_end = t_next + half_dur
        # Cubic ease-in-out: u<0.5 → 4u³  u≥0.5 → 1-(−2u+2)³/2
        u_expr = f"max(0\\,min(1\\,(t-{b_start:.4f})/{seg_dur:.4f}))"
        ease_expr = (
            f"if(lt({u_expr}\\,0.5)"
            f"\\,4*pow({u_expr}\\,3)"
            f"\\,1-pow(-2*{u_expr}+2\\,3)/2)"
        )
        lerp = f"({x_curr:.2f}+({x_next - x_curr:.2f})*{ease_expr})"
        if b_start <= t_curr + 0.001:
            seg = lerp
        else:
            seg = f"if(lt(t\\,{b_start:.4f})\\,{x_curr:.2f}\\,{lerp})"
        expr = f"if(lt(t\\,{b_end:.4f})\\,{seg}\\,{expr})"
    return expr


def build_keyframe_crop_filter(
    source_w: int, source_h: int,
    out_w: int, out_h: int,
    user_crop: dict,
) -> str:
    """
    Build complete FFmpeg crop+scale filter for keyframe-driven reframe.

    NOTES:
    - Crop window size is constant (shared zoom across keyframes)
    - Output is exactly out_w × out_h with explicit format=yuv420p
    - Uses Lanczos for high-quality scaling
    - NO black bars (crop is always exactly target aspect)
    """
    norm = normalize_user_crop(user_crop)
    if norm is None:
        raise ValueError("Invalid user_crop")

    keyframes = norm["keyframes"]
    zoom = norm.get("zoom", 1.0)
    transition = norm.get("transition", "smooth")
    transition_dur = float(norm.get("transition_dur", 0.4))

    crop_w, crop_h = compute_crop_dims(source_w, source_h, out_w, out_h, zoom)

    x_expr = _build_piecewise_expr(
        keyframes, "x", source_w, crop_w, transition, transition_dur
    )
    y_expr = _build_piecewise_expr(
        keyframes, "y", source_h, crop_h, transition, transition_dur
    )

    return (
        f"crop={crop_w}:{crop_h}:{x_expr}:{y_expr},"
        f"scale={out_w}:{out_h}:flags=lanczos,"
        f"setsar=1,"
        f"format=yuv420p"
    )


def build_precise_crop_filter(
    source_w: int, source_h: int,
    out_w: int, out_h: int,
    x_pct: float, y_pct: float,
    zoom: float = 1.0,
) -> str:
    """Single-point precise crop (constant X, Y). No black bars."""
    crop_w, crop_h = compute_crop_dims(source_w, source_h, out_w, out_h, zoom)
    cx = float(x_pct) * source_w
    cy = float(y_pct) * source_h
    crop_x = max(0, min(source_w - crop_w, int(cx - crop_w / 2))) // 2 * 2
    crop_y = max(0, min(source_h - crop_h, int(cy - crop_h / 2))) // 2 * 2
    return (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={out_w}:{out_h}:flags=lanczos,"
        f"setsar=1,"
        f"format=yuv420p"
    )


# =============================================================================
# Validation / clamping
# =============================================================================
def aspect_ratio_to_float(aspect: str) -> float:
    if ":" in aspect:
        w, h = aspect.split(":")
        return float(w) / float(h)
    return float(aspect)


def validate_user_crop(
    x_pct: float, y_pct: float,
    source_w: int, source_h: int,
    target_aspect: float, zoom: float = 1.0,
) -> tuple[float, float, float]:
    """Clamp single (x, y, zoom) so the crop window stays inside source."""
    zoom = max(1.0, min(4.0, float(zoom)))
    source_aspect = source_w / source_h
    if source_aspect >= target_aspect:
        crop_h = source_h / zoom
        crop_w = crop_h * target_aspect
    else:
        crop_w = source_w / zoom
        crop_h = crop_w / target_aspect
    half_w_pct = (crop_w / 2) / source_w
    half_h_pct = (crop_h / 2) / source_h
    x_pct = max(half_w_pct, min(1.0 - half_w_pct, float(x_pct)))
    y_pct = max(half_h_pct, min(1.0 - half_h_pct, float(y_pct)))
    return x_pct, y_pct, zoom


def validate_keyframes(
    keyframes: list[dict],
    source_w: int, source_h: int,
    target_aspect: float, zoom: float,
    clip_duration: float,
) -> list[dict]:
    """Clamp every keyframe; sort; ensure first is at t=0; dedupe near-duplicates."""
    cleaned = []
    for kf in keyframes:
        try:
            t = max(0.0, min(clip_duration, float(kf["t"])))
            x = float(kf.get("x_pct", 0.5))
            y = float(kf.get("y_pct", 0.5))
        except (KeyError, TypeError, ValueError):
            continue
        x, y, _ = validate_user_crop(x, y, source_w, source_h, target_aspect, zoom)
        entry = {"t": t, "x_pct": x, "y_pct": y}
        if "transition_dur_in" in kf:
            try:
                entry["transition_dur_in"] = max(0.05, min(2.0, float(kf["transition_dur_in"])))
            except (TypeError, ValueError):
                pass
        cleaned.append(entry)
    cleaned.sort(key=lambda k: k["t"])
    if cleaned and cleaned[0]["t"] > 0.001:
        cleaned.insert(0, {**cleaned[0], "t": 0.0})
    deduped = []
    for kf in cleaned:
        if deduped and abs(deduped[-1]["t"] - kf["t"]) < 0.05:
            continue
        deduped.append(kf)
    return deduped


# =============================================================================
# Frame extraction (for frontend preview)
# =============================================================================
def extract_source_frame(
    source_video: Path, timestamp_sec: float,
    output_jpg: Path, max_width: int = 1280,
) -> bool:
    """Extract a single frame at timestamp_sec, save as JPEG."""
    if not source_video.exists():
        return False
    output_jpg.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{timestamp_sec:.3f}",
        "-i", str(source_video),
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "3",
        str(output_jpg),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        if result.returncode != 0:
            return False
        return output_jpg.exists() and output_jpg.stat().st_size > 0
    except Exception:
        return False


def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(video_path)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        w_str, h_str = result.stdout.strip().split("x")
        return int(w_str), int(h_str)
    except Exception:
        return (0, 0)