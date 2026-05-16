#!/usr/bin/env python3
"""
scripts/test_conversation_pace.py

Phase 2B.2 validation: load Phase 2B.1 JSON outputs, run the conversation_pace
pipeline, and emit per-video 4-row timeline PNGs + JSON to debug_output_2b2/.

Expects (in /app/scripts/debug_output_2b1/):
  {stem}_speakers.json   — active_speaker_timeline from Phase 2B.1
  {stem}_tracks.json     — face_tracking from Phase 2B.1

Expects test videos at /app/test_videos/{stem}.mp4 for dimension probing.

Outputs per video (in /app/scripts/debug_output_2b2/):
  {stem}_pace.json          — compute_pace_timeline() output
  {stem}_keyframes.json     — place_adaptive_keyframes() output
  {stem}_pace_timeline.png  — 4-row visualisation at 200 DPI
  summary.json              — aggregate across all videos
"""
import json
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

INPUT_DIR  = Path("/app/scripts/debug_output_2b1")
OUTPUT_DIR = Path("/app/scripts/debug_output_2b2")
VIDEO_DIR  = Path("/app/test_videos")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, "/app")

from backend.pipeline.conversation_pace import (
    detect_speaker_changes,
    compute_pace_timeline,
    place_adaptive_keyframes,
)

PACE_COLORS = {"slow": "#4e92c7", "medium": "#f0a500", "fast": "#d93b3b"}

STEMS = [
    "01_single_speaker",
    "02_podcast_2person",
    "03_panel_4person",
    "04_screenshare",
    "05_lowlight",
]


def _get_video_dimensions(video_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(video_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0 or not r.stdout.strip():
        return 0, 0
    parts = r.stdout.strip().split("x")
    if len(parts) != 2:
        return 0, 0
    return int(parts[0]), int(parts[1])


def _restore_face_tracking_types(face_tracking: list[dict]) -> None:
    """Convert bbox lists back to tuples in-place (JSON loses tuple type)."""
    for frame in face_tracking:
        for face in frame["faces"]:
            if isinstance(face["bbox"], list):
                face["bbox"] = tuple(face["bbox"])
            face["landmarks"] = [
                tuple(kp) if isinstance(kp, list) else kp
                for kp in face["landmarks"]
            ]


def _plot_video(
    stem: str,
    speaker_timeline: list[dict],
    pace_timeline: list[dict],
    change_events: list[dict],
    keyframes: list[dict],
    duration_sec: float,
) -> None:
    seconds   = [e["second"] for e in speaker_timeline]
    track_ids = [e["active_track_id"] for e in speaker_timeline]

    unique_ids = sorted(set(t for t in track_ids if t is not None))
    id_to_int  = {tid: i for i, tid in enumerate(unique_ids)}
    cmap       = plt.cm.get_cmap("tab20", max(1, len(unique_ids)))

    fig, axes = plt.subplots(4, 1, figsize=(22, 11), sharex=True)
    fig.suptitle(f"Phase 2B.2 — {stem}", fontsize=13, fontweight="bold")

    # ── Row 1: Active speaker track_id ──────────────────────────────────────
    ax1 = axes[0]
    for sec, tid in zip(seconds, track_ids):
        color = cmap(id_to_int[tid]) if tid is not None else "#dddddd"
        ax1.bar(sec, 1, width=1, color=color, align="edge", linewidth=0)
    ax1.set_ylabel("Track", fontsize=8)
    ax1.set_yticks([])
    ax1.set_ylim(0, 1)
    ax1.set_title(
        f"Active speaker track_id  ({len(unique_ids)} unique tracks)", fontsize=9, pad=2
    )

    # ── Row 2: Debounced speaker change events ───────────────────────────────
    ax2 = axes[1]
    ax2.set_facecolor("#fafafa")
    for ev in change_events:
        ax2.axvline(ev["second"], color="#e74c3c", linewidth=1.2, alpha=0.85)
        ax2.text(
            ev["second"] + 0.4, 0.55,
            f'{ev["from_track"]}→{ev["to_track"]}',
            fontsize=5.5, va="center", color="#c0392b",
        )
    ax2.set_ylim(0, 1)
    ax2.set_yticks([])
    ax2.set_ylabel("Events", fontsize=8)
    ax2.set_title(
        f"Debounced speaker changes (min_hold=2s)  —  {len(change_events)} events",
        fontsize=9, pad=2,
    )

    # ── Row 3: Pace label per second ─────────────────────────────────────────
    ax3 = axes[2]
    for entry in pace_timeline:
        ax3.bar(
            entry["second"], 1, width=1,
            color=PACE_COLORS.get(entry["pace"], "#aaaaaa"),
            align="edge", linewidth=0,
        )
    patches = [mpatches.Patch(color=v, label=k) for k, v in PACE_COLORS.items()]
    ax3.legend(handles=patches, loc="upper right", fontsize=7, ncol=3, framealpha=0.8)
    ax3.set_ylabel("Pace", fontsize=8)
    ax3.set_yticks([])
    ax3.set_ylim(0, 1)
    counts = {p: sum(1 for e in pace_timeline if e["pace"] == p) for p in PACE_COLORS}
    ax3.set_title(
        f"Pace (10s window)  slow={counts['slow']}s  medium={counts['medium']}s  fast={counts['fast']}s",
        fontsize=9, pad=2,
    )

    # ── Row 4: Keyframe placements ───────────────────────────────────────────
    ax4 = axes[3]
    ax4.set_facecolor("#eef4ff")
    for kf in keyframes:
        ax4.axvline(kf["t"], color="#2471a3", linewidth=1.6)
        ax4.text(
            kf["t"] + 0.5, 0.62,
            f't={kf["t"]:.1f}\n({kf["x_pct"]:.2f},{kf["y_pct"]:.2f})',
            fontsize=5.5, va="center", color="#1a5276",
        )
    ax4.set_ylim(0, 1)
    ax4.set_yticks([])
    ax4.set_ylabel("Keyframes", fontsize=8)
    ax4.set_xlabel("Time (seconds)", fontsize=8)
    ax4.set_title(f"Adaptive keyframes placed: {len(keyframes)}", fontsize=9, pad=2)

    x_max = max(duration_sec, seconds[-1] + 1) if seconds else 30
    ax4.set_xlim(0, x_max)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    out = OUTPUT_DIR / f"{stem}_pace_timeline.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  PNG  → {out.name}")


def process_video(stem: str) -> dict:
    print(f"\n{'='*62}\n  {stem}\n{'='*62}")

    speakers_path = INPUT_DIR / f"{stem}_speakers.json"
    tracks_path   = INPUT_DIR / f"{stem}_tracks.json"

    if not speakers_path.exists():
        print(f"  SKIP — {speakers_path.name} missing"); return {}
    if not tracks_path.exists():
        print(f"  SKIP — {tracks_path.name} missing"); return {}

    with speakers_path.open() as f:
        speaker_timeline: list[dict] = json.load(f)
    with tracks_path.open() as f:
        face_tracking: list[dict] = json.load(f)

    _restore_face_tracking_types(face_tracking)

    video_path = VIDEO_DIR / f"{stem}.mp4"
    sw, sh = _get_video_dimensions(video_path) if video_path.exists() else (0, 0)
    if sw == 0:
        print("  WARNING: no video dimensions — face centres will fall back to 0.5, 0.5")

    duration_sec = float(speaker_timeline[-1]["second"] + 1) if speaker_timeline else 30.0

    # ── Step 1: detect speaker changes ──────────────────────────────────────
    print(f"  [1/4] detect_speaker_changes ... ", end="", flush=True)
    change_events = detect_speaker_changes(speaker_timeline, min_hold_seconds=2)
    print(f"{len(change_events)} events")
    for ev in change_events[:8]:
        print(f"        t={ev['second']}s  {ev['from_track']} → {ev['to_track']}")
    if len(change_events) > 8:
        print(f"        ... ({len(change_events) - 8} more)")

    # ── Step 2: pace timeline ────────────────────────────────────────────────
    print(f"  [2/4] compute_pace_timeline ... ", end="", flush=True)
    pace_timeline = compute_pace_timeline(
        speaker_timeline, window_seconds=10, min_hold_seconds=2
    )
    counts = {p: sum(1 for e in pace_timeline if e["pace"] == p) for p in ("slow", "medium", "fast")}
    print(f"slow={counts['slow']}s  medium={counts['medium']}s  fast={counts['fast']}s")

    # ── Step 3: adaptive keyframes ───────────────────────────────────────────
    print(f"  [3/4] place_adaptive_keyframes ... ", end="", flush=True)
    keyframes = place_adaptive_keyframes(
        speaker_timeline,
        face_tracking,
        clip_start_sec=0.0,
        clip_end_sec=duration_sec,
        source_width=sw,
        source_height=sh,
    )
    print(f"{len(keyframes)} keyframes")
    for i, kf in enumerate(keyframes):
        print(f"        [{i}] t={kf['t']:.2f}s  x={kf['x_pct']:.3f}  y={kf['y_pct']:.3f}")

    # ── Step 4: plot ─────────────────────────────────────────────────────────
    print(f"  [4/4] plotting PNG ... ", end="", flush=True)
    _plot_video(stem, speaker_timeline, pace_timeline, change_events, keyframes, duration_sec)

    # ── Save JSON ─────────────────────────────────────────────────────────────
    with (OUTPUT_DIR / f"{stem}_pace.json").open("w") as f:
        json.dump(pace_timeline, f, indent=2)
    with (OUTPUT_DIR / f"{stem}_keyframes.json").open("w") as f:
        json.dump(keyframes, f, indent=2)
    print(f"  JSON → {stem}_pace.json + {stem}_keyframes.json")

    return {
        "stem":         stem,
        "duration_sec": round(duration_sec, 1),
        "source_wh":    [sw, sh],
        "change_events": len(change_events),
        "pace":         counts,
        "keyframes":    len(keyframes),
        "keyframe_list": keyframes,
    }


def main():
    print("\nPhase 2B.2 test — conversation_pace.py")
    print(f"Input:  {INPUT_DIR}")
    print(f"Output: {OUTPUT_DIR}")

    summary = []
    for stem in STEMS:
        result = process_video(stem)
        if result:
            summary.append(result)

    with (OUTPUT_DIR / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*62}")
    print(f"Results ({len(summary)} videos):")
    header = f"  {'stem':30s}  {'changes':>7}  {'slow':>5}  {'med':>5}  {'fast':>5}  {'kframes':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in summary:
        print(
            f"  {r['stem']:30s}  "
            f"{r['change_events']:>7}  "
            f"{r['pace']['slow']:>5}  "
            f"{r['pace']['medium']:>5}  "
            f"{r['pace']['fast']:>5}  "
            f"{r['keyframes']:>7}"
        )
    print(f"\nAll outputs in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
