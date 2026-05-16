"""
scripts/test_active_speaker.py

Phase 2B.1 visual verification — audio activity + active speaker detection.

For each test video:
  1. analyze_audio_activity()     → {stem}_audio.json
  2. track_faces_across_frames()  → {stem}_tracks.json
  3. compute_active_speaker_timeline() → {stem}_speakers.json
  4. matplotlib timeline PNG (3 rows, dpi=200):
       Row 1: Audio energy (fill_between, light blue)
       Row 2: Voice activity blocks (step fill, green=voice, light grey=silence)
       Row 3: Active speaker track_id (bar per second, colour-coded by track_id, grey=None)
  5. summary.json

Run from inside the backend container:
    docker exec clipwise-backend-1 python /tmp/test_active_speaker.py

Output directory: /app/scripts/debug_output_2b1/  (or host path for local runs)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_active_speaker")

# ---------------------------------------------------------------------------
# Paths — container takes priority
# ---------------------------------------------------------------------------
_CONTAINER_VIDEO_DIR = Path("/app/test_videos")
_CONTAINER_DEBUG_DIR = Path("/app/scripts/debug_output_2b1")
_HOST_VIDEO_DIR      = Path("C:/Resume Projects/Clipwise/test_videos")
_HOST_DEBUG_DIR      = Path("C:/Resume Projects/Clipwise/clipwise/scripts/debug_output_2b1")

if _CONTAINER_VIDEO_DIR.exists():
    VIDEO_DIR = _CONTAINER_VIDEO_DIR
    DEBUG_DIR = _CONTAINER_DEBUG_DIR
else:
    VIDEO_DIR = _HOST_VIDEO_DIR
    DEBUG_DIR = _HOST_DEBUG_DIR

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

# Colour palette for track_id bars (cycles if more tracks than colours)
_TRACK_COLORS = [
    "#4C72B0",  # blue
    "#DD8452",  # orange
    "#55A868",  # green
    "#C44E52",  # red
    "#8172B2",  # purple
    "#937860",  # brown
    "#DA8BC3",  # pink
    "#8C8C8C",  # grey (used for track_id None — kept separate)
]
_NONE_COLOR = "#CCCCCC"


# ---------------------------------------------------------------------------
# JSON serialization helper (tuples → lists)
# ---------------------------------------------------------------------------

def _to_serializable(obj):
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, list):
        return [_to_serializable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Timeline plot
# ---------------------------------------------------------------------------

def _make_timeline_png(
    stem: str,
    audio_activity: dict,
    speakers: list[dict],
    out_path: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np

    energy   = audio_activity.get("energy", [])
    is_voice = audio_activity.get("is_voice", [])
    window_ms = audio_activity.get("window_ms", 100)
    duration_sec = audio_activity.get("duration_sec", 0.0)

    n_windows = len(energy)
    t_energy  = np.arange(n_windows) * window_ms / 1000.0     # seconds

    fig, axes = plt.subplots(
        3, 1,
        figsize=(20, 9),
        dpi=200,
        sharex=True,
        gridspec_kw={"height_ratios": [2, 1.2, 2]},
    )
    fig.suptitle(f"Phase 2B.1 — {stem}", fontsize=14, fontweight="bold", y=0.98)

    # --- Row 1: Audio energy ---
    ax1 = axes[0]
    ax1.fill_between(t_energy, energy, color="#AED6F1", alpha=0.9, linewidth=0)
    ax1.plot(t_energy, energy, color="#2E86C1", linewidth=0.4, alpha=0.7)
    ax1.set_ylabel("Energy (norm.)", fontsize=9)
    ax1.set_ylim(0, 1.05)
    ax1.set_title("Audio Energy", fontsize=9, loc="left", pad=2)
    ax1.grid(axis="y", linewidth=0.4, alpha=0.4)

    # --- Row 2: Voice activity ---
    ax2 = axes[1]
    if is_voice:
        voice_arr = np.array(is_voice, dtype=float)
        t_voice   = np.arange(len(is_voice)) * window_ms / 1000.0
        # Step fill: voice = green, silence = light grey
        ax2.fill_between(
            t_voice, voice_arr,
            step="post",
            color="#27AE60", alpha=0.85, linewidth=0,
        )
        ax2.fill_between(
            t_voice, 1 - voice_arr,
            step="post",
            color="#E0E0E0", alpha=0.5, linewidth=0,
        )
    ax2.set_ylabel("Voice", fontsize=9)
    ax2.set_ylim(0, 1.05)
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels(["silence", "voice"], fontsize=7)
    ax2.set_title("Voice Activity (webrtcvad, aggressiveness=2)", fontsize=9, loc="left", pad=2)

    # --- Row 3: Active speaker ---
    ax3 = axes[2]

    # Collect all unique track_ids (excluding None)
    track_ids_seen: list[int] = sorted(
        {e["active_track_id"] for e in speakers if e["active_track_id"] is not None}
    )
    tid_color: dict[int, str] = {
        tid: _TRACK_COLORS[i % len(_TRACK_COLORS)]
        for i, tid in enumerate(track_ids_seen)
    }

    for entry in speakers:
        sec   = entry["second"]
        tid   = entry["active_track_id"]
        color = tid_color.get(tid, _NONE_COLOR) if tid is not None else _NONE_COLOR
        ax3.bar(sec + 0.5, 1.0, width=1.0, color=color, alpha=0.85, linewidth=0)

    # Legend patches
    patches = [
        mpatches.Patch(color=tid_color[tid], label=f"Track {tid}")
        for tid in track_ids_seen
    ]
    patches.append(mpatches.Patch(color=_NONE_COLOR, label="None (no speaker)"))
    ax3.legend(
        handles=patches,
        loc="upper right",
        fontsize=7,
        framealpha=0.7,
        ncol=min(len(patches), 4),
    )

    ax3.set_ylabel("Track ID", fontsize=9)
    ax3.set_ylim(0, 1.3)
    ax3.set_yticks([])
    ax3.set_title("Active Speaker per Second", fontsize=9, loc="left", pad=2)
    ax3.set_xlabel("Time (seconds)", fontsize=9)
    ax3.set_xlim(0, max(duration_sec, len(speakers)))
    ax3.grid(axis="x", linewidth=0.4, alpha=0.4)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(str(out_path), dpi=200, bbox_inches="tight")
    plt.close(fig)
    logger.info(f"  Timeline PNG → {out_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure /app is on sys.path when running as a copied script in /tmp/
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")

    try:
        from backend.pipeline.audio_activity import analyze_audio_activity
        from backend.pipeline.active_speaker import (
            track_faces_across_frames,
            compute_active_speaker_timeline,
        )
    except ImportError as e:
        logger.error(f"Cannot import pipeline modules: {e}")
        logger.error("Run inside the backend container.")
        sys.exit(1)

    if not VIDEO_DIR.exists():
        logger.error(f"Video directory not found: {VIDEO_DIR}")
        sys.exit(1)

    videos = sorted(
        p for p in VIDEO_DIR.iterdir()
        if p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not videos:
        logger.error(f"No video files found in {VIDEO_DIR}")
        sys.exit(1)

    logger.info(f"Found {len(videos)} video(s)  |  output → {DEBUG_DIR}")

    summary: dict = {}

    for video_path in videos:
        stem = video_path.stem
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {video_path.name}")

        video_summary: dict = {"status": "ok", "files": []}

        try:
            # ----------------------------------------------------------------
            # Step 1: Audio activity
            # ----------------------------------------------------------------
            logger.info("Step 1/3 — analyze_audio_activity")
            audio_activity = analyze_audio_activity(video_path, job_id=stem)

            audio_json_path = DEBUG_DIR / f"{stem}_audio.json"
            audio_json_path.write_text(
                json.dumps(_to_serializable(audio_activity), indent=2),
                encoding="utf-8",
            )
            video_summary["files"].append(audio_json_path.name)
            video_summary["duration_sec"]  = audio_activity["duration_sec"]
            video_summary["window_count"]  = audio_activity["window_count"]
            voice_count = sum(audio_activity["is_voice"])
            video_summary["voice_windows"] = voice_count
            video_summary["voice_pct"]     = round(
                voice_count / audio_activity["window_count"] * 100
                if audio_activity["window_count"] else 0.0,
                1,
            )
            video_summary["voice_segments"] = len(audio_activity["voice_segments"])
            logger.info(f"  Audio: {audio_activity['duration_sec']:.1f}s  "
                        f"voice={voice_count}/{audio_activity['window_count']} windows  "
                        f"({video_summary['voice_pct']}%)")

            # ----------------------------------------------------------------
            # Step 2: Face tracking (cached — skip if tracks JSON already exists)
            # ----------------------------------------------------------------
            tracks_json_path = DEBUG_DIR / f"{stem}_tracks.json"
            if tracks_json_path.exists():
                logger.info("Step 2/3 — track_faces_across_frames  [CACHED — loading existing JSON]")
                face_tracking = json.loads(tracks_json_path.read_text(encoding="utf-8"))
                # Restore tuples from JSON lists for bbox/landmarks
                for fr in face_tracking:
                    for face in fr["faces"]:
                        face["bbox"] = tuple(face["bbox"])
                        face["landmarks"] = [tuple(lm) for lm in face["landmarks"]]
            else:
                logger.info("Step 2/3 — track_faces_across_frames")
                face_tracking = track_faces_across_frames(video_path)
                tracks_json_path.write_text(
                    json.dumps(_to_serializable(face_tracking), indent=2),
                    encoding="utf-8",
                )

            video_summary["files"].append(tracks_json_path.name)
            total_detections = sum(len(fr["faces"]) for fr in face_tracking)
            track_ids_seen   = {f["track_id"] for fr in face_tracking for f in fr["faces"]}
            video_summary["sampled_frames"]   = len(face_tracking)
            video_summary["total_detections"] = total_detections
            video_summary["unique_tracks"]    = len(track_ids_seen)
            logger.info(f"  Tracking: {len(face_tracking)} frames  "
                        f"{total_detections} detections  "
                        f"{len(track_ids_seen)} unique tracks")

            # ----------------------------------------------------------------
            # Step 3: Active speaker timeline
            # ----------------------------------------------------------------
            logger.info("Step 3/3 — compute_active_speaker_timeline")
            speakers = compute_active_speaker_timeline(face_tracking, audio_activity)

            speakers_json_path = DEBUG_DIR / f"{stem}_speakers.json"
            speakers_json_path.write_text(
                json.dumps(_to_serializable(speakers), indent=2),
                encoding="utf-8",
            )
            video_summary["files"].append(speakers_json_path.name)

            # Reasoning breakdown
            reasoning_counts: dict[str, int] = {}
            for entry in speakers:
                r = entry["reasoning"]
                reasoning_counts[r] = reasoning_counts.get(r, 0) + 1
            video_summary["reasoning_counts"] = reasoning_counts
            logger.info(f"  Speaker timeline: {len(speakers)} seconds  "
                        f"reasoning={reasoning_counts}")

            # ----------------------------------------------------------------
            # Step 4: Timeline PNG
            # ----------------------------------------------------------------
            png_path = DEBUG_DIR / f"{stem}_timeline.png"
            _make_timeline_png(stem, audio_activity, speakers, png_path)
            video_summary["files"].append(png_path.name)

        except Exception as e:
            logger.error(f"  ERROR processing {video_path.name}: {e}", exc_info=True)
            video_summary["status"] = "error"
            video_summary["error"]  = str(e)

        summary[stem] = video_summary

    # Write summary.json
    summary_path = DEBUG_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"\n{'='*60}")
    logger.info(f"Summary → {summary_path}")

    # Human-readable report
    logger.info("\n=== PHASE 2B.1 REPORT ===")
    total_files = 0
    for vid, stats in summary.items():
        if stats.get("status") == "error":
            logger.info(f"  {vid}: ERROR — {stats.get('error', '?')}")
            continue
        n_files = len(stats.get("files", []))
        total_files += n_files
        logger.info(
            f"  {vid}: "
            f"{stats.get('duration_sec', '?'):.1f}s  |  "
            f"voice={stats.get('voice_pct', '?')}%  |  "
            f"tracks={stats.get('unique_tracks', '?')}  |  "
            f"files={n_files}"
        )

    logger.info(f"\nTotal output files: {total_files} + 1 summary.json")
    logger.info("Phase 2B.1 visual verification complete — inspect debug_output_2b1/")


if __name__ == "__main__":
    main()
