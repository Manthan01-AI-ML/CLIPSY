"""
scripts/test_adaptive_transitions.py — Phase 2B.3 render test

Renders 2 MP4s per video (slow + medium/fast segment) using the new
cubic ease-in-out pan with per-segment transition_dur_in.

Inputs:
  /app/scripts/debug_output_2b1/{stem}_speakers.json
  /app/scripts/debug_output_2b1/{stem}_tracks.json
  /app/scripts/debug_output_2b2/{stem}_pace.json

Outputs per video:
  /app/scripts/debug_output_2b3/{stem}_slow.mp4
  /app/scripts/debug_output_2b3/{stem}_slow_filter.txt
  /app/scripts/debug_output_2b3/{stem}_fast.mp4          (skipped if no medium/fast region)
  /app/scripts/debug_output_2b3/{stem}_fast_filter.txt   (skipped if no medium/fast region)
  /app/scripts/debug_output_2b3/summary.json
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

# Hardcoded absolute paths — avoids __file__ resolution issues when run from /tmp/
INPUT_2B1  = Path("/app/scripts/debug_output_2b1")
INPUT_2B2  = Path("/app/scripts/debug_output_2b2")
OUTPUT_DIR = Path("/app/scripts/debug_output_2b3")
VIDEO_DIR  = Path("/app/test_videos")

CLIP_SECONDS = 10   # render window length in seconds

sys.path.insert(0, "/app")

from backend.pipeline.reframe import build_keyframe_crop_filter, get_video_dimensions
from backend.pipeline.conversation_pace import place_adaptive_keyframes


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def restore_face_tracking(tracking: list) -> list:
    """JSON serialises tuples as lists; restore bbox tuples for _face_center_pct."""
    for frame in tracking:
        for face in frame.get("faces", []):
            if isinstance(face.get("bbox"), list):
                face["bbox"] = tuple(face["bbox"])
    return tracking


# ---------------------------------------------------------------------------
# Window-finding helpers
# ---------------------------------------------------------------------------

def _best_window(
    pace_timeline: list[dict],
    target_paces: set[str],
    window_sec: int,
) -> Optional[tuple[int, int]]:
    """
    Return the (start, end) window of window_sec seconds that contains the
    most seconds with pace in target_paces.  Returns None if target_paces
    has no representatives in the timeline.
    """
    seconds = sorted(row["second"] for row in pace_timeline)
    pace_map = {row["second"]: row["pace"] for row in pace_timeline}

    target = {s for s in seconds if pace_map.get(s) in target_paces}
    if not target:
        return None

    best_start: Optional[int] = None
    best_count = 0

    for s in seconds:
        count = sum(1 for sec in seconds if s <= sec < s + window_sec and sec in target)
        if count > best_count:
            best_count = count
            best_start = s

    if best_start is None:
        return None
    return (best_start, best_start + window_sec)


def find_slow_window(pace_timeline: list[dict]) -> Optional[tuple[int, int]]:
    return _best_window(pace_timeline, {"slow"}, CLIP_SECONDS)


def find_fast_window(pace_timeline: list[dict]) -> Optional[tuple[int, int]]:
    return _best_window(pace_timeline, {"medium", "fast"}, CLIP_SECONDS)


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------

def transition_dur_distribution(keyframes: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for kf in keyframes:
        val = kf.get("transition_dur_in")
        if val is not None:
            key = str(val)
            dist[key] = dist.get(key, 0) + 1
    return dist


# ---------------------------------------------------------------------------
# FFmpeg render
# ---------------------------------------------------------------------------

def render_clip(
    source_path: Path,
    start_sec: float,
    end_sec: float,
    vf_filter: str,
    output_path: Path,
) -> tuple[bool, str, float]:
    """
    Returns (success, ffmpeg_stderr, elapsed_sec).
    success = returncode 0 AND output file exists AND non-zero size.
    """
    t0 = time.time()
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-ss", f"{start_sec:.3f}",
        "-to", f"{end_sec:.3f}",
        "-i", str(source_path),
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-an",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    elapsed = round(time.time() - t0, 2)
    success = (
        result.returncode == 0
        and output_path.exists()
        and output_path.stat().st_size > 0
    )
    return success, result.stderr, elapsed


# ---------------------------------------------------------------------------
# Per-segment render
# ---------------------------------------------------------------------------

def render_segment(
    label: str,
    stem: str,
    window: tuple[int, int],
    speaker_timeline: list,
    face_tracking: list,
    video_path: Path,
    sw: int, sh: int,
    out_w: int, out_h: int,
) -> dict:
    start_sec = float(window[0])
    end_sec   = float(window[1])

    keyframes = place_adaptive_keyframes(
        speaker_timeline, face_tracking,
        clip_start_sec=start_sec,
        clip_end_sec=end_sec,
        source_width=sw,
        source_height=sh,
    )

    # Clamp transition_dur_in to the slice window (Fix 3 — slice path)
    slice_duration = end_sec - start_sec
    for kf in keyframes:
        if "transition_dur_in" not in kf:
            continue
        max_dur = max(0.05, 2.0 * (slice_duration - 0.1 - kf["t"]))
        if kf["transition_dur_in"] > max_dur:
            kf["transition_dur_in"] = round(max_dur, 3)

    print(f"    [{label}] [{start_sec:.0f}–{end_sec:.0f}s]  keyframes={len(keyframes)}")

    user_crop = {
        "version": 2,
        "keyframes": keyframes,
        "zoom": 1.0,
        "transition": "smooth",
        "transition_dur": 0.4,
    }
    vf = build_keyframe_crop_filter(sw, sh, out_w, out_h, user_crop)

    (OUTPUT_DIR / f"{stem}_{label}_filter.txt").write_text(vf, encoding="utf-8")

    output_mp4 = OUTPUT_DIR / f"{stem}_{label}.mp4"
    success, stderr_out, elapsed = render_clip(
        video_path, start_sec, end_sec, vf, output_mp4
    )

    entry: dict = {
        "segment_range": list(window),
        "keyframe_count": len(keyframes),
        "transition_dur_distribution": transition_dur_distribution(keyframes),
        "render_time_sec": elapsed,
        "success": success,
    }

    if success:
        entry["output_kb"] = round(output_mp4.stat().st_size / 1024, 1)
        print(f"    [{label}] OK  {entry['output_kb']} KB  {elapsed}s")
    else:
        entry["error"] = "returncode != 0 or 0-byte output"
        entry["ffmpeg_stderr_tail"] = stderr_out.splitlines()[-15:]
        print(f"    [{label}] FAILED")
        for line in entry["ffmpeg_stderr_tail"]:
            print(f"      {line}")

    return entry


# ---------------------------------------------------------------------------
# Per-video pipeline
# ---------------------------------------------------------------------------

def process_video(stem: str) -> dict:
    print(f"\n{'='*60}\n  {stem}\n{'='*60}")

    entry: dict = {
        "stem": stem,
        "source_dims": None,
        "output_dims": None,
        "slow_segment": None,
        "fast_segment": None,
        "errors": [],
    }

    video_path = VIDEO_DIR / f"{stem}.mp4"
    if not video_path.exists():
        msg = f"Source video not found: {video_path}"
        print(f"  ERROR: {msg}")
        entry["errors"].append(msg)
        return entry

    for path in (
        INPUT_2B1 / f"{stem}_speakers.json",
        INPUT_2B1 / f"{stem}_tracks.json",
        INPUT_2B2 / f"{stem}_pace.json",
    ):
        if not path.exists():
            msg = f"Missing input: {path}"
            print(f"  ERROR: {msg}")
            entry["errors"].append(msg)
            return entry

    speaker_timeline = load_json(INPUT_2B1 / f"{stem}_speakers.json")
    face_tracking    = restore_face_tracking(load_json(INPUT_2B1 / f"{stem}_tracks.json"))
    pace_timeline    = load_json(INPUT_2B2 / f"{stem}_pace.json")

    sw, sh = get_video_dimensions(video_path)
    if sw == 0 or sh == 0:
        msg = f"get_video_dimensions returned ({sw},{sh}) — ffprobe failed; skipping renders"
        print(f"  ERROR: {msg}")
        entry["errors"].append(msg)
        return entry
    out_w, out_h = (1080, 1920) if sw >= 1080 else (540, 960)
    print(f"  Source: {sw}x{sh}  →  Output: {out_w}x{out_h}")
    entry["source_dims"] = [sw, sh]
    entry["output_dims"] = [out_w, out_h]

    slow_window = find_slow_window(pace_timeline)
    fast_window = find_fast_window(pace_timeline)
    print(f"  Slow window: {slow_window}")
    print(f"  Fast window: {fast_window}")

    if slow_window:
        try:
            entry["slow_segment"] = render_segment(
                "slow", stem, slow_window,
                speaker_timeline, face_tracking,
                video_path, sw, sh, out_w, out_h,
            )
            if not entry["slow_segment"]["success"]:
                entry["errors"].append("slow render failed")
        except Exception as e:
            msg = f"Exception in slow render: {e}"
            print(f"  ERROR: {msg}")
            traceback.print_exc()
            entry["errors"].append(msg)
    else:
        print("  No slow window found — skipping")

    if fast_window:
        try:
            entry["fast_segment"] = render_segment(
                "fast", stem, fast_window,
                speaker_timeline, face_tracking,
                video_path, sw, sh, out_w, out_h,
            )
            if not entry["fast_segment"]["success"]:
                entry["errors"].append("fast render failed")
        except Exception as e:
            msg = f"Exception in fast render: {e}"
            print(f"  ERROR: {msg}")
            traceback.print_exc()
            entry["errors"].append(msg)
    else:
        print("  No medium/fast window found — skipping fast render")

    return entry


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    stems = [
        "01_single_speaker",
        "02_podcast_2person",
        "03_panel_4person",
        "04_screenshare",
        "05_lowlight",
    ]

    summary: dict = {"videos": {}, "total_files": 0, "output_files": []}

    for stem in stems:
        try:
            summary["videos"][stem] = process_video(stem)
        except Exception as e:
            print(f"  FATAL for {stem}: {e}")
            traceback.print_exc()
            summary["videos"][stem] = {"stem": stem, "errors": [f"Fatal: {e}"]}

    all_output = sorted(f.name for f in OUTPUT_DIR.iterdir())
    # write summary first, then count it
    summary_path = OUTPUT_DIR / "summary.json"
    summary["output_files"] = [f for f in all_output if f != "summary.json"]
    summary["total_files"]  = len(all_output) + 1  # +1 for summary.json itself

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Summary → {summary_path}")
    print(f"Total output files (incl. summary.json): {summary['total_files']}")


if __name__ == "__main__":
    main()
