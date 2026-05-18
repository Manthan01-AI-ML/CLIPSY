"""
backend/pipeline/active_speaker.py

Phase 2B.1: Assign "who is speaking" per second by combining face tracking
(IoU-based) with audio VAD and lip-movement scoring.

Public API:
  track_faces_across_frames(video_path, *, sample_fps=None) -> list[dict]
    Returns per-sampled-frame face tracks with IoU-linked track_ids.

  compute_active_speaker_timeline(face_tracking, audio_activity) -> list[dict]
    Returns per-second active speaker assignment.

MediaPipe keypoint index 3 = mouth center (verified in container, mediapipe 0.10.35).
All keypoint labels are None — positional indexing required.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MOUTH_KP_INDEX       = 3      # verified: label=None, positional index for mouth center
IOU_MATCH_THRESHOLD  = 0.5    # min IoU to link a detection to an existing track
LIP_VARIANCE_NORMALIZER = 200.0   # px² — std dev ≈ 14px = clearly talking (calibrated 720p–1080p)
LIP_DOMINANCE_RATIO  = 1.5    # dominant speaker must score >= 1.5× next speaker's lip score

SPATIAL_GRID_W = 4              # BUG-008: divide frame into 4×4 grid for cut-robust tracking
SPATIAL_GRID_H = 4
SPATIAL_REGION_TIMEOUT_SEC = 3.0  # region "remembers" track_id for this long after last seen


class ActiveSpeakerError(Exception):
    pass


# ---------------------------------------------------------------------------
# Adaptive sample rate
# ---------------------------------------------------------------------------

def _adaptive_sample_fps(duration_sec: float) -> int:
    """
    Choose sample_fps based on video duration.
    Short videos: high temporal resolution for smooth lip tracking.
    Long videos: sparse coverage to avoid extremely long run times.
    """
    if duration_sec <= 300:
        return 4
    elif duration_sec <= 900:
        return 2
    else:
        return 1


# ---------------------------------------------------------------------------
# IoU for bounding boxes (x, y, w, h)
# ---------------------------------------------------------------------------

def _iou(b1: tuple, b2: tuple) -> float:
    """Intersection over Union for two (x, y, w, h) boxes."""
    x1, y1, w1, h1 = b1
    x2, y2, w2, h2 = b2

    ix1 = max(x1, x2)
    iy1 = max(y1, y2)
    ix2 = min(x1 + w1, x2 + w2)
    iy2 = min(y1 + h1, y2 + h2)

    inter_w = max(0, ix2 - ix1)
    inter_h = max(0, iy2 - iy1)
    inter   = inter_w * inter_h

    union = w1 * h1 + w2 * h2 - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Lip movement score
# ---------------------------------------------------------------------------

def compute_lip_movement_score(
    landmarks_history: list[list[tuple[int, int]]],
) -> float:
    """
    Score lip movement as variance of mouth Y-coordinate across recent frames.

    landmarks_history: list of keypoint lists, one per frame. Each keypoint
    list is [(kp_x, kp_y), ...]. Index MOUTH_KP_INDEX (3) is mouth center.

    Returns 0.0–1.0.  0.0 if fewer than 2 frames or mouth keypoint absent.
    Normalizer: 200 px² ≈ (14px std dev)² — clearly talking threshold.
    """
    mouth_y_values: list[float] = []
    for kps in landmarks_history:
        if len(kps) > MOUTH_KP_INDEX:
            mouth_y_values.append(float(kps[MOUTH_KP_INDEX][1]))

    if len(mouth_y_values) < 2:
        return 0.0

    variance = float(np.var(mouth_y_values))
    return min(1.0, variance / LIP_VARIANCE_NORMALIZER)


# ---------------------------------------------------------------------------
# Video duration via ffprobe
# ---------------------------------------------------------------------------

def _get_video_duration(video_path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise ActiveSpeakerError(
            f"ffprobe failed on {video_path.name}: {result.stderr.strip()[:200]}"
        )
    return float(result.stdout.strip())


def _get_video_dimensions(video_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise ActiveSpeakerError(
            f"ffprobe dims failed on {video_path.name}: {result.stderr.strip()[:200]}"
        )
    w_str, h_str = result.stdout.strip().split("x")
    return int(w_str), int(h_str)


def _extract_frame(
    video_path: Path, timestamp_sec: float, W: int, H: int
) -> Optional[np.ndarray]:
    """Extract a single BGR frame at timestamp_sec via ffmpeg raw pipe."""
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{timestamp_sec:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        return None

    expected = W * H * 3
    if result.returncode != 0 or len(result.stdout) < expected:
        return None

    arr = np.frombuffer(result.stdout[:expected], dtype=np.uint8)
    return arr.reshape((H, W, 3))


# ---------------------------------------------------------------------------
# Spatial-region helper (BUG-008: cut-robust track continuity)
# ---------------------------------------------------------------------------

def _spatial_region_id(bbox: tuple, frame_w: int, frame_h: int) -> tuple[int, int]:
    """Map a bbox center to a (col, row) grid cell index."""
    x, y, w, h = bbox
    cx = x + w / 2
    cy = y + h / 2
    col = min(SPATIAL_GRID_W - 1, max(0, int(cx / frame_w * SPATIAL_GRID_W)))
    row = min(SPATIAL_GRID_H - 1, max(0, int(cy / frame_h * SPATIAL_GRID_H)))
    return (col, row)


# ---------------------------------------------------------------------------
# Face tracking across frames (IoU + spatial-region)
# ---------------------------------------------------------------------------

def track_faces_across_frames(
    video_path: Path,
    *,
    sample_fps: Optional[int] = None,
) -> list[dict]:
    """
    Sample frames from video_path at sample_fps and run face detection,
    linking detections across frames via IoU.

    sample_fps is chosen adaptively from _adaptive_sample_fps() unless
    overridden. Covers the full video timeline at lower temporal resolution
    for long videos.

    Returns:
      list[dict] — one entry per sampled frame:
        {
          "timestamp_sec": float,
          "faces": [
            {
              "track_id": int,
              "bbox": (x, y, w, h),
              "confidence": float,
              "landmarks": [(x, y), ...]  # 6 keypoints, index 3 = mouth
            }
          ]
        }
    """
    try:
        from backend.pipeline.face_detection import detect_faces_with_retry
    except ImportError as e:
        raise ActiveSpeakerError(f"Cannot import face_detection: {e}") from e

    duration_sec = _get_video_duration(video_path)
    W, H = _get_video_dimensions(video_path)

    effective_fps = sample_fps if sample_fps is not None else _adaptive_sample_fps(duration_sec)
    interval_sec  = 1.0 / effective_fps

    expected_frames = int(duration_sec * effective_fps)
    logger.info(
        f"track_faces_across_frames: {video_path.name}  "
        f"sample_fps={effective_fps}  duration={duration_sec:.1f}s  "
        f"(expected ~{expected_frames} frames)"
    )

    # Build list of timestamps to sample
    timestamps: list[float] = []
    t = 0.0
    while t < duration_sec - 0.1:
        timestamps.append(min(t, duration_sec - 0.1))
        t += interval_sec

    # Track state
    prev_faces: list[dict] = []    # faces from the previous frame, with track_ids
    next_track_id = 0
    results: list[dict] = []
    region_track_history: dict[tuple[int, int], tuple[int, float]] = {}

    for frame_idx, ts in enumerate(timestamps):
        frame = _extract_frame(video_path, ts, W, H)
        if frame is None:
            logger.warning(f"  [frame {frame_idx}  t={ts:.1f}s] extraction failed — skipping")
            prev_faces = []
            continue

        raw_faces, _ = detect_faces_with_retry(frame)

        # IoU + spatial-region track assignment (BUG-008 fix)
        assigned: list[dict] = []
        used_prev: set[int] = set()

        for raw in raw_faces:
            region = _spatial_region_id(raw["bbox"], W, H)

            # Step 1: try IoU match against previous frame (handles smooth motion)
            best_iou = 0.0
            best_prev_idx = -1
            for pi, prev in enumerate(prev_faces):
                if pi in used_prev:
                    continue
                score = _iou(raw["bbox"], prev["bbox"])
                if score > best_iou:
                    best_iou = score
                    best_prev_idx = pi

            if best_iou >= IOU_MATCH_THRESHOLD and best_prev_idx >= 0:
                track_id = prev_faces[best_prev_idx]["track_id"]
                used_prev.add(best_prev_idx)
            else:
                # Step 2: IoU failed (likely camera cut). Try spatial-region match.
                region_match = region_track_history.get(region)
                if region_match is not None:
                    prior_tid, prior_ts = region_match
                    if (ts - prior_ts) <= SPATIAL_REGION_TIMEOUT_SEC:
                        track_id = prior_tid
                    else:
                        track_id = next_track_id
                        next_track_id += 1
                else:
                    track_id = next_track_id
                    next_track_id += 1

            # Update region memory regardless of how track_id was chosen
            region_track_history[region] = (track_id, ts)

            assigned.append({
                "track_id":   track_id,
                "bbox":       raw["bbox"],
                "confidence": raw["confidence"],
                "landmarks":  raw["landmarks"],
            })

        prev_faces = assigned

        results.append({
            "timestamp_sec": round(ts, 3),
            "faces": assigned,
        })

        if frame_idx > 0 and frame_idx % 50 == 0:
            n_faces = len(assigned)
            logger.info(
                f"  frame {frame_idx}/{len(timestamps)}  "
                f"t={ts:.1f}s  faces={n_faces}"
            )

    logger.info(
        f"track_faces_across_frames complete: {len(results)} frames sampled, "
        f"tracks used: {next_track_id}, regions occupied: {len(region_track_history)}"
    )
    return results


# ---------------------------------------------------------------------------
# Active speaker timeline (per second)
# ---------------------------------------------------------------------------

def compute_active_speaker_timeline(
    face_tracking: list[dict],
    audio_activity: dict,
) -> list[dict]:
    """
    Assign "who is speaking" per second.

    Algorithm per second S:
      1. If no audio activity (is_voice windows covering S are all False): "audio_inactive"
      2. If no face tracks cover second S: "no_faces"
      3. If exactly one face track: "only_face"
      4. Compute lip_movement_score per track using landmarks from frames in [S, S+1).
         If dominant track score >= LIP_DOMINANCE_RATIO × next highest: "lip_movement_dominant"
      5. Else fall back to largest face (max bbox area): "largest_face"

    Returns:
      list[dict]:
        {
          "second": int,
          "active_track_id": Optional[int],  # None if audio_inactive or no_faces
          "confidence": float,               # lip score (0–1) or 0.0 for fallback
          "reasoning": str
        }
    """
    is_voice: list[bool] = audio_activity.get("is_voice", [])
    window_ms: int = audio_activity.get("window_ms", 100)
    duration_sec: float = audio_activity.get("duration_sec", 0.0)

    windows_per_second = 1000 // window_ms  # = 10 at 100ms

    # Index face_tracking frames by second
    frames_by_second: dict[int, list[dict]] = {}
    for frame in face_tracking:
        sec = int(frame["timestamp_sec"])
        frames_by_second.setdefault(sec, []).append(frame)

    # Collect per-track landmark history across the full tracking window
    # to compute lip movement score per track per second
    # We look back up to 1 second worth of frames for each track
    track_landmarks_by_second: dict[int, dict[int, list]] = {}
    for frame in face_tracking:
        sec = int(frame["timestamp_sec"])
        for face in frame["faces"]:
            tid = face["track_id"]
            track_landmarks_by_second.setdefault(sec, {}).setdefault(tid, [])
            track_landmarks_by_second[sec][tid].append(face["landmarks"])

    n_seconds = max(int(duration_sec) + 1, max(frames_by_second.keys(), default=0) + 1)
    timeline: list[dict] = []

    for sec in range(n_seconds):
        # Is audio active this second?
        win_start = sec * windows_per_second
        win_end   = win_start + windows_per_second
        second_voice_windows = is_voice[win_start:win_end]
        audio_active = any(second_voice_windows) if second_voice_windows else False

        if not audio_active:
            timeline.append({
                "second": sec,
                "active_track_id": None,
                "confidence": 0.0,
                "reasoning": "audio_inactive",
            })
            continue

        # Collect faces visible this second
        second_frames = frames_by_second.get(sec, [])
        if not second_frames:
            timeline.append({
                "second": sec,
                "active_track_id": None,
                "confidence": 0.0,
                "reasoning": "no_faces",
            })
            continue

        # Unique track_ids in this second
        track_ids_this_second: set[int] = set()
        for fr in second_frames:
            for face in fr["faces"]:
                track_ids_this_second.add(face["track_id"])

        track_ids = list(track_ids_this_second)

        if len(track_ids) == 0:
            timeline.append({
                "second": sec,
                "active_track_id": None,
                "confidence": 0.0,
                "reasoning": "no_faces",
            })
            continue

        if len(track_ids) == 1:
            # Get confidence from latest face detection
            latest_face = next(
                (f for fr in reversed(second_frames) for f in fr["faces"]
                 if f["track_id"] == track_ids[0]),
                None,
            )
            conf = latest_face["confidence"] if latest_face else 0.0
            timeline.append({
                "second": sec,
                "active_track_id": track_ids[0],
                "confidence": round(conf, 3),
                "reasoning": "only_face",
            })
            continue

        # Lip movement scores per track
        lip_scores: dict[int, float] = {}
        for tid in track_ids:
            landmarks_history = track_landmarks_by_second.get(sec, {}).get(tid, [])
            lip_scores[tid] = compute_lip_movement_score(landmarks_history)

        sorted_tracks = sorted(lip_scores.items(), key=lambda kv: kv[1], reverse=True)
        best_tid, best_score = sorted_tracks[0]
        second_score = sorted_tracks[1][1] if len(sorted_tracks) > 1 else 0.0

        if best_score > 0.0 and (second_score == 0.0 or best_score >= LIP_DOMINANCE_RATIO * second_score):
            timeline.append({
                "second": sec,
                "active_track_id": best_tid,
                "confidence": round(best_score, 3),
                "reasoning": "lip_movement_dominant",
            })
            continue

        # Fallback: largest face by bbox area
        largest_tid = None
        largest_area = 0
        for fr in second_frames:
            for face in fr["faces"]:
                _, _, w, h = face["bbox"]
                if w * h > largest_area:
                    largest_area = w * h
                    largest_tid = face["track_id"]

        timeline.append({
            "second": sec,
            "active_track_id": largest_tid,
            "confidence": 0.0,
            "reasoning": "largest_face",
        })

    return timeline
