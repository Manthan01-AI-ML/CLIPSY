"""
backend/pipeline/face_detection.py

Phase 2A.1: Corrective fixes — two real models, tiered confidence retry, CLAHE fallback.

Changes from Phase 2A:
  - Full-range detector now uses blaze_face_full_range.tflite (genuinely different
    model, not an alias of short-range). Short-range = selfie/close-up (≤ ~2m);
    full-range = group shots, panels, camera > ~2m away.
  - Two separate FaceDetector singletons with different NMS thresholds:
      short-range: min_suppression_threshold=0.3 (default — standard for close-ups)
      full-range:  min_suppression_threshold=0.1 (permissive — adjacent panel faces
                   must not be merged by NMS)
  - Both detectors init at min_detection_confidence=0.2; all confidence
    filtering is done at the call site, not inside _run_detection().
  - New detect_faces_with_retry(frame) → (list[dict], str): cascades through
      short(≥0.5, large-face check) → full(≥0.3) → CLAHE+full(≥0.3)
    and returns the detection path taken ("short"/"full"/"clahe+full"/"none").
  - New enhance_for_detection(frame): CLAHE on LAB L-channel for low-light.
  - auto_select_model() kept for backward compat but superseded by the
    first-pass size check in detect_faces_with_retry().

FaceDetectorOptions verified fields (mediapipe 0.10.35, confirmed via help()):
  base_options, running_mode, min_detection_confidence, min_suppression_threshold,
  result_callback. There is NO num_faces/max_results parameter — the detector
  returns ALL detections that pass the two init-time thresholds.

Model files (downloaded to /tmp/mediapipe_models/ on first use, ~1 MB each):
  blaze_face_short_range.tflite   — selfie/close-up
  blaze_face_full_range.tflite    — group/panel/wide
"""
from __future__ import annotations

import logging
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model download / cache
# ---------------------------------------------------------------------------
_MODEL_CACHE_DIR = Path("/tmp/mediapipe_models")

_SHORT_RANGE_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_detector/blaze_face_short_range/float16/latest/"
    "blaze_face_short_range.tflite"
)
_FULL_RANGE_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_detector/blaze_face_full_range/float16/latest/"
    "blaze_face_full_range.tflite"
)
_SHORT_RANGE_PATH = _MODEL_CACHE_DIR / "blaze_face_short_range.tflite"
_FULL_RANGE_PATH  = _MODEL_CACHE_DIR / "blaze_face_full_range.tflite"


def _ensure_model(url: str, dest: Path) -> None:
    """Download model if not cached. Raises RuntimeError on failure — never falls back silently."""
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading MediaPipe model: {dest.name} …")
    try:
        urllib.request.urlretrieve(url, str(dest))
        size_kb = dest.stat().st_size // 1024
        logger.info(f"Downloaded: {dest.name} ({size_kb} KB)")
    except Exception as e:
        raise RuntimeError(f"Failed to download MediaPipe model from {url}: {e}") from e


# ---------------------------------------------------------------------------
# Singleton state — two distinct detectors, different models and NMS settings
# ---------------------------------------------------------------------------
_short_range_detector = None  # blaze_face_short_range, NMS=0.3
_full_range_detector  = None  # blaze_face_full_range,  NMS=0.1


def _get_short_range_detector():
    global _short_range_detector
    if _short_range_detector is None:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        _ensure_model(_SHORT_RANGE_URL, _SHORT_RANGE_PATH)
        base_opts = mp_python.BaseOptions(model_asset_path=str(_SHORT_RANGE_PATH))
        options = mp_vision.FaceDetectorOptions(
            base_options=base_opts,
            min_detection_confidence=0.2,   # low gate; caller applies tiered thresholds
            min_suppression_threshold=0.3,  # standard NMS for close-up faces
        )
        _short_range_detector = mp_vision.FaceDetector.create_from_options(options)
        logger.info("MediaPipe FaceDetector loaded: short-range (selfie/≤2m, NMS=0.3)")
    return _short_range_detector


def _get_full_range_detector():
    global _full_range_detector
    if _full_range_detector is None:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        _ensure_model(_FULL_RANGE_URL, _FULL_RANGE_PATH)
        base_opts = mp_python.BaseOptions(model_asset_path=str(_FULL_RANGE_PATH))
        options = mp_vision.FaceDetectorOptions(
            base_options=base_opts,
            min_detection_confidence=0.2,   # low gate
            min_suppression_threshold=0.1,  # permissive NMS — adjacent panel faces must survive
        )
        _full_range_detector = mp_vision.FaceDetector.create_from_options(options)
        logger.info("MediaPipe FaceDetector loaded: full-range (group/panel/>2m, NMS=0.1)")
    return _full_range_detector


# ---------------------------------------------------------------------------
# Core inference (no post-filter — callers apply thresholds)
# ---------------------------------------------------------------------------

def _run_detection(detector, frame: np.ndarray, model_name: str) -> list[dict]:
    """
    Run a Tasks API FaceDetector. Returns ALL detections above the init-time
    threshold (0.2). No confidence post-filter here — callers decide their cutoff.
    """
    import mediapipe as mp

    H, W = frame.shape[:2]
    try:
        rgb = frame[:, :, ::-1].copy()  # BGR → RGB; .copy() ensures C-contiguous
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = detector.detect(mp_image)
    except Exception as e:
        logger.warning(f"MediaPipe detection failed ({model_name}): {e}")
        return []

    if not result or not result.detections:
        logger.debug(f"MediaPipe {model_name}: 0 raw detections in {W}x{H}")
        return []

    faces: list[dict] = []
    for det in result.detections:
        score = float(det.categories[0].score) if det.categories else 0.0

        bb = det.bounding_box
        x = max(0, int(bb.origin_x))
        y = max(0, int(bb.origin_y))
        w = min(W - x, int(bb.width))
        h = min(H - y, int(bb.height))

        landmarks = [
            (int(kp.x * W), int(kp.y * H))
            for kp in (det.keypoints or [])
        ]
        faces.append({"bbox": (x, y, w, h), "confidence": score, "landmarks": landmarks})

    logger.debug(f"MediaPipe {model_name}: {len(faces)} raw detection(s) in {W}x{H}")
    return faces


# ---------------------------------------------------------------------------
# Single-pass public functions (direct model access)
# ---------------------------------------------------------------------------

def detect_faces_mediapipe(
    frame: np.ndarray,
    min_confidence: float = 0.5,
) -> list[dict]:
    """
    Short-range BlazeFace (selfie / close-up ≤ ~2m).

    Best for single-speaker talking-head and 2-person podcast content.
    Caller sets min_confidence; detector init gate is 0.2.

    Returns list[dict] with "bbox" (x,y,w,h), "confidence", "landmarks".
    Empty list if no faces pass threshold or model unavailable.
    """
    if frame is None or frame.size == 0:
        return []
    try:
        detector = _get_short_range_detector()
    except Exception as e:
        logger.warning(f"Short-range detector unavailable: {e}")
        return []
    raw = _run_detection(detector, frame, "short-range")
    return [f for f in raw if f["confidence"] >= min_confidence]


def detect_faces_mediapipe_full_range(
    frame: np.ndarray,
    min_confidence: float = 0.3,
) -> list[dict]:
    """
    Full-range BlazeFace (group shots / panels / camera > ~2m).

    Uses a genuinely different .tflite model than short-range, with permissive
    NMS (0.1) so adjacent faces in tight group shots are not merged.
    Default min_confidence is 0.3 (lower than short-range) because faces at
    distance have inherently lower confidence scores.

    Returns list[dict] with "bbox" (x,y,w,h), "confidence", "landmarks".
    Empty list if no faces pass threshold or model unavailable.
    """
    if frame is None or frame.size == 0:
        return []
    try:
        detector = _get_full_range_detector()
    except Exception as e:
        logger.warning(f"Full-range detector unavailable: {e}")
        return []
    raw = _run_detection(detector, frame, "full-range")
    return [f for f in raw if f["confidence"] >= min_confidence]


# ---------------------------------------------------------------------------
# CLAHE preprocessing for low-light
# ---------------------------------------------------------------------------

def enhance_for_detection(frame: np.ndarray) -> np.ndarray:
    """
    Apply CLAHE to the L channel (LAB color space) to boost contrast in
    low-light frames before running face detection.

    clipLimit=2.0 and tileGridSize=(8,8) are conservative — enough to lift
    shadow detail without over-amplifying noise. Input frame is not modified.
    Returns a new BGR frame.
    """
    import cv2
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_eq = clahe.apply(l_ch)
    return cv2.cvtColor(cv2.merge([l_eq, a_ch, b_ch]), cv2.COLOR_LAB2BGR)


# ---------------------------------------------------------------------------
# Multi-pass cascade — main entry point for production use
# ---------------------------------------------------------------------------

_LARGE_FACE_AREA_RATIO = 0.08  # bbox area / frame area threshold for "close-up"


def detect_faces_with_retry(frame: np.ndarray) -> tuple[list[dict], str]:
    """
    Cascading face detection with tiered confidence and CLAHE fallback.

    Pass 1 — short-range at high confidence (0.5):
      If any detected face covers >= 8% of frame area, the content is close-up
      — return immediately as "short". Avoids running full-range unnecessarily
      for the common single-speaker talking-head case.

    Pass 2 — full-range at lower confidence (0.3):
      Handles group shots, panels, screenshare facecams, and cases where Pass 1
      found faces but all were small (< 8% area, likely a wide shot).

    Pass 3 — CLAHE enhanced frame + full-range (0.3):
      Last resort for low-light content where raw inference produces near-zero
      confidence scores. Contrast enhancement can lift a 0.18 score to 0.35+.

    Returns:
        (faces, detection_path) where detection_path is one of:
        "short" | "full" | "clahe+full" | "none"
    """
    if frame is None or frame.size == 0:
        return [], "none"

    H, W = frame.shape[:2]
    frame_area = max(1, W * H)

    # Pass 1: short-range, high-confidence only
    short_faces = detect_faces_mediapipe(frame, min_confidence=0.5)
    if short_faces:
        largest_ratio = max(
            f["bbox"][2] * f["bbox"][3] for f in short_faces
        ) / frame_area
        if largest_ratio >= _LARGE_FACE_AREA_RATIO:
            return short_faces, "short"
        # Faces found but all small — fall through to full-range which handles
        # wide/group content better; don't return the small short-range hits.

    # Pass 2: full-range, permissive confidence
    full_faces = detect_faces_mediapipe_full_range(frame, min_confidence=0.3)
    if full_faces:
        return full_faces, "full"

    # Pass 3: CLAHE + full-range
    enhanced = enhance_for_detection(frame)
    clahe_faces = detect_faces_mediapipe_full_range(enhanced, min_confidence=0.3)
    if clahe_faces:
        return clahe_faces, "clahe+full"

    return [], "none"


# ---------------------------------------------------------------------------
# Backward-compat helpers
# ---------------------------------------------------------------------------

def auto_select_model(frame: np.ndarray) -> str:
    """
    Dimension-based model hint: "short" or "full".

    Kept for callers that need a hint without running inference. In production
    code, prefer detect_faces_with_retry() which uses actual detection results
    to decide when to escalate from short-range to full-range.
    """
    if frame is None or frame.size == 0:
        return "short"
    H, W = frame.shape[:2]
    aspect = W / H if H > 0 else 1.0
    if H > W:
        return "short"
    if aspect > 2.5:
        return "full"
    if (W * H) > 2_000_000 and aspect > 1.6:
        return "full"
    return "short"


def detect_faces_smart(
    frame: np.ndarray,
    min_confidence: float = 0.5,
) -> list[dict]:
    """
    Convenience wrapper: runs detect_faces_with_retry() and returns only faces.

    Phase 2B entry point from smart_crop.py (callers don't need the path string).
    min_confidence is accepted for API compatibility but the cascade internally
    uses fixed thresholds (0.5 short / 0.3 full / 0.3 clahe+full).
    """
    faces, _ = detect_faces_with_retry(frame)
    return faces


def legacy_compatible_detect(
    frame_bgr: Optional[np.ndarray],
    min_face_size: int = 60,
) -> list[tuple]:
    """
    Drop-in replacement for smart_crop._FaceDetector.detect_in_frame().

    Signature and return shape are identical:
      list[tuple[int, int, int, int, float]] = [(x, y, w, h, score), ...]

    score = confidence × size_bonus, matching Haar scorer convention so that
    _cluster_speakers() and auto_keyframes_from_detection() need no changes.
    """
    if frame_bgr is None or (hasattr(frame_bgr, "size") and frame_bgr.size == 0):
        return []

    H, W = frame_bgr.shape[:2]
    faces, _ = detect_faces_with_retry(frame_bgr)

    result: list[tuple] = []
    for face in faces:
        x, y, w, h = face["bbox"]
        if w < min_face_size or h < min_face_size:
            continue
        conf = face["confidence"]
        size_factor = (w * h) / (W * H) if W * H > 0 else 0.0
        score = conf * (1.0 + min(1.0, size_factor * 100))
        result.append((int(x), int(y), int(w), int(h), float(score)))

    return result
