"""
backend/pipeline/conversation_pace.py

Phase 2B.2: Classify conversation pace from the active speaker timeline and
place adaptive crop keyframes at debounced speaker-change events.

Public API:
  detect_speaker_changes(active_speaker_timeline, min_hold_seconds=2) -> list[dict]
  classify_pace_window(change_events, current_second, window_seconds=10) -> str
  compute_pace_timeline(active_speaker_timeline, *, window_seconds=10, min_hold_seconds=2) -> list[dict]
  place_adaptive_keyframes(active_speaker_timeline, face_tracking, *, min_hold_seconds=2,
                            clip_start_sec=0.0, clip_end_sec=None,
                            source_width=0, source_height=0) -> list[dict]

BUG-008 (track fragmentation on camera cuts) is intentionally NOT fixed here;
debouncing in detect_speaker_changes() makes all downstream functions robust to it.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Pace thresholds — changes per window_seconds window
_PACE_MEDIUM_MIN = 3   # 0-2 = "slow", 3-5 = "medium"
_PACE_FAST_MIN   = 6   # 6+  = "fast"

# Minimum dwell time between keyframes, matches smart_crop.py convention
_MIN_KEYFRAME_DWELL_SEC = 1.5

# Pan duration (seconds) emitted as transition_dur_in per pace bucket
_PACE_TO_TRANSITION_DUR = {
    "slow":   0.30,
    "medium": 0.20,
    "fast":   0.12,
}


class ConversationPaceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Speaker change detection (debounced)
# ---------------------------------------------------------------------------

def detect_speaker_changes(
    active_speaker_timeline: list[dict],
    min_hold_seconds: int = 2,
) -> list[dict]:
    """
    Debounced speaker-change detector.  A change event fires only when a new
    active_track_id holds for >= min_hold_seconds consecutive seconds.

    None entries (audio_inactive / no_faces) pause tracking without generating
    events and reset the pending candidate, so a brief silence followed by the
    same speaker does not produce a false change.

    Returns:
      list[dict]:
        {
          "second": int,          # absolute second when change was confirmed
          "from_track": int,
          "to_track": int,
        }
    """
    events: list[dict] = []
    current_stable: Optional[int] = None
    candidate: Optional[int] = None
    candidate_start_sec: int = 0

    for entry in active_speaker_timeline:
        sec = entry["second"]
        tid = entry["active_track_id"]

        if tid is None:
            # Silence / no face: pause tracking, reset candidate
            candidate = None
            continue

        if current_stable is None:
            # Bootstrap: first speaker seen — no event
            current_stable = tid
            candidate = None
            continue

        if tid == current_stable:
            # Stable speaker confirmed again: clear pending candidate
            candidate = None
            continue

        if tid != candidate:
            # New potential challenger — start timing
            candidate = tid
            candidate_start_sec = sec
            continue

        # tid == candidate: check whether it has held long enough
        if sec - candidate_start_sec + 1 >= min_hold_seconds:
            events.append({
                "second": sec,
                "from_track": current_stable,
                "to_track": tid,
            })
            current_stable = tid
            candidate = None

    return events


# ---------------------------------------------------------------------------
# Pace window classifier
# ---------------------------------------------------------------------------

def classify_pace_window(
    change_events: list[dict],
    current_second: int,
    window_seconds: int = 10,
) -> str:
    """
    Count debounced speaker-change events in the window
    [current_second - window_seconds, current_second] and return a pace label.

    Returns: "slow" | "medium" | "fast"
    """
    window_start = current_second - window_seconds
    count = sum(
        1 for ev in change_events
        if window_start <= ev["second"] <= current_second
    )
    if count >= _PACE_FAST_MIN:
        return "fast"
    if count >= _PACE_MEDIUM_MIN:
        return "medium"
    return "slow"


# ---------------------------------------------------------------------------
# Per-second pace timeline
# ---------------------------------------------------------------------------

def compute_pace_timeline(
    active_speaker_timeline: list[dict],
    *,
    window_seconds: int = 10,
    min_hold_seconds: int = 2,
) -> list[dict]:
    """
    Compute a per-second pace label for the full active speaker timeline.

    Returns:
      list[dict]:
        {
          "second": int,
          "pace": str,          # "slow" | "medium" | "fast"
          "change_count": int,  # events in the trailing window
        }
    """
    events = detect_speaker_changes(active_speaker_timeline, min_hold_seconds=min_hold_seconds)

    result: list[dict] = []
    for entry in active_speaker_timeline:
        sec = entry["second"]
        window_start = sec - window_seconds
        count = sum(1 for ev in events if window_start <= ev["second"] <= sec)
        if count >= _PACE_FAST_MIN:
            pace = "fast"
        elif count >= _PACE_MEDIUM_MIN:
            pace = "medium"
        else:
            pace = "slow"
        result.append({
            "second": sec,
            "pace": pace,
            "change_count": count,
        })

    return result


# ---------------------------------------------------------------------------
# Helpers for keyframe placement
# ---------------------------------------------------------------------------

def _face_center_pct(
    track_id: int,
    abs_second: int,
    face_tracking: list[dict],
    source_width: int,
    source_height: int,
) -> tuple[float, float]:
    """
    Average the bbox centre for track_id across all sampled frames in abs_second.
    Returns (x_pct, y_pct) in [0, 1].  Falls back to (0.5, 0.5) when no data.
    """
    if source_width <= 0 or source_height <= 0:
        return 0.5, 0.5

    xs: list[float] = []
    ys: list[float] = []
    for frame in face_tracking:
        ts = frame["timestamp_sec"]
        if abs_second <= ts < abs_second + 1:
            for face in frame["faces"]:
                if face["track_id"] == track_id:
                    x, y, w, h = face["bbox"]
                    xs.append(x + w / 2)
                    ys.append(y + h / 2)

    if not xs:
        return 0.5, 0.5

    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    return (
        round(min(1.0, max(0.0, cx / source_width)), 4),
        round(min(1.0, max(0.0, cy / source_height)), 4),
    )


def _largest_face_center_at_second(
    abs_second: int,
    face_tracking: list[dict],
    source_width: int,
    source_height: int,
) -> tuple[float, float]:
    """
    Bbox centre of the largest face (by area) at abs_second across all tracks.
    Falls back to (0.5, 0.5) if no faces are found.
    """
    if source_width <= 0 or source_height <= 0:
        return 0.5, 0.5

    best_area = 0
    best_cx: Optional[float] = None
    best_cy: Optional[float] = None
    for frame in face_tracking:
        ts = frame["timestamp_sec"]
        if abs_second <= ts < abs_second + 1:
            for face in frame["faces"]:
                x, y, w, h = face["bbox"]
                area = w * h
                if area > best_area:
                    best_area = area
                    best_cx = x + w / 2
                    best_cy = y + h / 2

    if best_cx is None:
        return 0.5, 0.5
    return (
        round(min(1.0, max(0.0, best_cx / source_width)), 4),
        round(min(1.0, max(0.0, best_cy / source_height)), 4),
    )


def _has_faces_at_second(abs_second: int, face_tracking: list[dict]) -> bool:
    return any(
        len(fr.get("faces", [])) > 0
        for fr in face_tracking
        if abs_second <= fr["timestamp_sec"] < abs_second + 1
    )


# ---------------------------------------------------------------------------
# Adaptive keyframe placement
# ---------------------------------------------------------------------------

def place_adaptive_keyframes(
    active_speaker_timeline: list[dict],
    face_tracking: list[dict],
    *,
    min_hold_seconds: int = 2,
    clip_start_sec: float = 0.0,
    clip_end_sec: Optional[float] = None,
    source_width: int = 0,
    source_height: int = 0,
) -> list[dict]:
    """
    Place crop keyframes at debounced speaker-change events within the clip range.

    The timeline is filtered to [clip_start_sec, clip_end_sec) before change
    detection runs, so only changes inside the clip influence the output.
    The first keyframe is always at t=0 (clip-relative), positioned on the
    initial active speaker.

    Args:
      active_speaker_timeline: output of compute_active_speaker_timeline()
      face_tracking:           output of track_faces_across_frames()
      clip_start_sec:          absolute start of the clip in the source video (seconds)
      clip_end_sec:            absolute end; None = full timeline
      source_width/height:     source video dimensions for normalising bbox centres
      min_hold_seconds:        debounce hold length passed to detect_speaker_changes()

    Returns:
      list[dict]: [{"t": float, "x_pct": float, "y_pct": float, "transition_dur_in": float}, ...]
        t is clip-relative (0.0 = clip start), sorted ascending.
        First keyframe (t=0) has no transition_dur_in.
        All others carry transition_dur_in (seconds) derived from pace at that event second.
        Minimum 1.5 s dwell between consecutive keyframes is enforced.
    """
    # Resolve clip end
    if clip_end_sec is None:
        clip_end_sec = (
            float(active_speaker_timeline[-1]["second"] + 1)
            if active_speaker_timeline
            else clip_start_sec + 30.0
        )

    clip_start_int = int(clip_start_sec)
    clip_end_int   = int(clip_end_sec)
    clip_duration  = clip_end_sec - clip_start_sec

    # Filter timeline to clip range
    clip_timeline = [
        e for e in active_speaker_timeline
        if clip_start_int <= e["second"] < clip_end_int
    ]

    if not clip_timeline:
        logger.warning(
            "place_adaptive_keyframes: empty clip range "
            f"[{clip_start_sec:.1f}, {clip_end_sec:.1f}) — returning centre keyframe"
        )
        return [{"t": 0.0, "x_pct": 0.5, "y_pct": 0.5}]

    # t=0 keyframe: active track AT clip_start_int → largest face → scan forward → center
    start_entry = next((e for e in clip_timeline if e["second"] == clip_start_int), None)
    start_track = start_entry["active_track_id"] if start_entry else None
    start_track_has_data = (
        start_track is not None
        and any(
            face["track_id"] == start_track
            for frame in face_tracking
            if clip_start_int <= frame["timestamp_sec"] < clip_start_int + 1
            for face in frame.get("faces", [])
        )
    )

    if start_track_has_data:
        x0, y0 = _face_center_pct(start_track, clip_start_int, face_tracking, source_width, source_height)
    else:
        # No track data at clip start — scan forward for first second with any face.
        # (0.5, 0.5) is the last resort only when the entire clip has no detections.
        x0, y0 = 0.5, 0.5
        for scan_sec in range(clip_start_int, min(clip_start_int + 30, clip_end_int)):
            if _has_faces_at_second(scan_sec, face_tracking):
                x0, y0 = _largest_face_center_at_second(scan_sec, face_tracking, source_width, source_height)
                break

    keyframes: list[dict] = [{"t": 0.0, "x_pct": x0, "y_pct": y0}]

    # Speaker change events within clip range
    events = detect_speaker_changes(clip_timeline, min_hold_seconds=min_hold_seconds)

    # Build per-second pace lookup for transition_dur_in assignment
    pace_timeline = compute_pace_timeline(clip_timeline, min_hold_seconds=min_hold_seconds)
    pace_by_second: dict[int, str] = {row["second"]: row["pace"] for row in pace_timeline}

    for ev in events:
        t_clip = round(float(ev["second"]) - clip_start_sec, 3)

        if t_clip <= 0.0:
            continue

        # Enforce minimum dwell from last accepted keyframe
        if t_clip - keyframes[-1]["t"] < _MIN_KEYFRAME_DWELL_SEC:
            continue

        # Reject events too close to clip end
        if t_clip >= clip_duration - 0.1:
            continue

        x_pct, y_pct = _face_center_pct(
            ev["to_track"], ev["second"], face_tracking, source_width, source_height
        )
        pace = pace_by_second.get(ev["second"], "slow")
        keyframes.append({
            "t": t_clip,
            "x_pct": x_pct,
            "y_pct": y_pct,
            "transition_dur_in": _PACE_TO_TRANSITION_DUR[pace],
        })

    # Clamp transition_dur_in so pan completes >= 0.1s before full-video end.
    # kf["t"] is clip-relative; for the production full-video path (clip_start=0)
    # it equals the absolute video time, so this clamp correctly prevents
    # late-video pans from overshooting. For slice paths, Fix 3 in the render
    # script clamps relative to the slice end instead.
    video_duration = float(len(active_speaker_timeline))
    for kf in keyframes:
        if "transition_dur_in" not in kf:
            continue
        max_dur = max(0.05, 2.0 * (video_duration - 0.1 - kf["t"]))
        if kf["transition_dur_in"] > max_dur:
            kf["transition_dur_in"] = round(max_dur, 3)

    logger.info(
        f"place_adaptive_keyframes: clip [{clip_start_sec:.1f}–{clip_end_sec:.1f}s]  "
        f"change_events={len(events)}  keyframes_placed={len(keyframes)}"
    )
    return keyframes
