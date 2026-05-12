"""
backend/pipeline/smart_crop.py

Three-stage crop strategy for vertical conversion:

  1. USER OVERRIDE (highest priority) — when clip.meta.user_crop is set
     (via the Reframe modal), use those exact coordinates. Supports v1 single
     crop AND v2 keyframe timeline. No face detection.

  2. FACE-AWARE auto-detection — when no user override, sample frames and
     detect faces using a robust multi-cascade ensemble + multi-frame voting.

  3. FALLBACK — if all detection fails, use the explicit `fallback_position`
     ("center", "left", "right", etc.) from the user's job settings.

Backward compatible: render.py calls build_smart_crop_filter(...) without
user_crop. Reframe path passes user_crop with either v1 or v2 schema.

ROBUSTNESS UPGRADES (Session DETECT-V2):
  - 4-cascade ensemble: alt2 + default + alt + profile
  - Histogram equalization for low-light frames
  - Tightened minNeighbors=3 (was 5) — catches more valid faces, NMS dedupes
  - Mirrored profile detection for right-facing faces
  - Multi-frame clustering — same speaker across frames vote together
  - Score-weighted (cascade reliability + face size) speaker ranking
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Number of frames to sample across the clip for face detection
_SAMPLE_FRAME_COUNT = 8

# Min face size as a fraction of frame width (filters out tiny background faces)
_MIN_FACE_FRAC = 0.04


# =============================================================================
# Robust face detector (multi-cascade ensemble)
# =============================================================================
class _FaceDetector:
    """
    Multi-cascade Haar detector with NMS deduplication. Tuned for podcast/
    interview videos where speakers are clearly visible and ~10-30% of frame
    width.

    Detection scoring:
      - Each cascade has a reliability weight (alt2=1.0 best, profile=0.7 worst)
      - Larger faces score higher (closer to camera = "primary" speaker)
      - Final score combines both
    """

    CASCADE_FILES = [
        ('frontalface_alt2', 'haarcascade_frontalface_alt2.xml', 1.0),
        ('frontalface_default', 'haarcascade_frontalface_default.xml', 0.9),
        ('frontalface_alt', 'haarcascade_frontalface_alt.xml', 0.85),
        ('profileface', 'haarcascade_profileface.xml', 0.7),
    ]

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def _ensure_loaded(self):
        if self._loaded:
            return
        try:
            import cv2
        except ImportError:
            logger.warning("OpenCV not available")
            self.cascades = []
            self._loaded = True
            return

        self.cv2 = cv2
        self.cascades = []
        self.profile_cascade = None
        for name, fn, weight in self.CASCADE_FILES:
            try:
                c = cv2.CascadeClassifier(cv2.data.haarcascades + fn)
                if not c.empty():
                    self.cascades.append((name, c, weight))
                    if name == 'profileface':
                        self.profile_cascade = c
            except Exception as e:
                logger.warning(f"Cascade {name} failed to load: {e}")
        self._loaded = True
        logger.info(f"Smart crop: loaded {len(self.cascades)} face cascades")

    def detect_in_frame(self, frame_bgr, min_face_size: int = 60) -> list[tuple]:
        """
        Detect faces in a single BGR frame. Returns [(x, y, w, h, score), ...].

        DETECTION QUALITY V3:
          - CLAHE (Contrast Limited Adaptive Histogram Equalization) instead of
            global equalizeHist — much better in mixed lighting (one bright + one
            shadowed speaker, common in podcasts). Enhances local contrast.
          - Bilateral filter to reduce noise without blurring face features
          - Multi-scale: also run detection on a downscaled frame, scale results
            back up. Catches faces the original-resolution pass missed.
          - Skin-tone sanity filter: reject "faces" that have <15% skin-tone
            pixels (filters out face-like patterns in backgrounds, logos, etc.)
        """
        self._ensure_loaded()
        if not self.cascades or frame_bgr is None or frame_bgr.size == 0:
            return []

        cv2 = self.cv2
        try:
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            # CLAHE: way better than equalizeHist in podcast scenes with two
            # differently-lit speakers. Tile size 8x8 is the standard sweet spot.
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
            # Light bilateral denoise — preserves face edges, kills sensor noise
            gray = cv2.bilateralFilter(gray, d=5, sigmaColor=35, sigmaSpace=5)
        except Exception:
            return []

        H, W = frame_bgr.shape[:2]
        all_dets: list[tuple] = []

        # === Pass 1: original-resolution frontal cascades ===
        for name, cascade, weight in self.cascades:
            try:
                faces = cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.05,
                    minNeighbors=3,
                    minSize=(min_face_size, min_face_size),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                for (x, y, w, h) in faces:
                    size_factor = (w * h) / (W * H)
                    score = weight * (1.0 + min(1.0, size_factor * 100))
                    all_dets.append((int(x), int(y), int(w), int(h), float(score)))
            except Exception:
                continue

        # === Pass 2: downscaled frame (catches faces missed at full res due to
        # noise or compression artifacts; less precise but more recall) ===
        if W > 960:
            try:
                scale = 0.5
                small = cv2.resize(gray, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_AREA)
                small_min = max(30, int(min_face_size * scale))
                # Use only the most reliable cascade for the second pass
                primary = self.cascades[0][1] if self.cascades else None
                if primary is not None:
                    faces = primary.detectMultiScale(
                        small, scaleFactor=1.08, minNeighbors=3,
                        minSize=(small_min, small_min),
                        flags=cv2.CASCADE_SCALE_IMAGE,
                    )
                    for (x, y, w, h) in faces:
                        # Scale back up
                        x = int(x / scale); y = int(y / scale)
                        w = int(w / scale); h = int(h / scale)
                        size_factor = (w * h) / (W * H)
                        # Lower weight (0.7x) since lower-res = less reliable
                        score = 0.7 * (1.0 + min(1.0, size_factor * 100))
                        all_dets.append((x, y, w, h, float(score)))
            except Exception:
                pass

        # === Pass 3: profile cascade (left-facing + mirrored = right-facing) ===
        if self.profile_cascade is not None:
            try:
                faces = self.profile_cascade.detectMultiScale(
                    gray, scaleFactor=1.05, minNeighbors=3,
                    minSize=(min_face_size, min_face_size),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                for (x, y, w, h) in faces:
                    size_factor = (w * h) / (W * H)
                    score = 0.7 * (1.0 + min(1.0, size_factor * 100))
                    all_dets.append((int(x), int(y), int(w), int(h), float(score)))
            except Exception:
                pass
            try:
                mirrored = cv2.flip(gray, 1)
                faces = self.profile_cascade.detectMultiScale(
                    mirrored, scaleFactor=1.05, minNeighbors=3,
                    minSize=(min_face_size, min_face_size),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                for (x, y, w, h) in faces:
                    real_x = W - int(x) - int(w)
                    size_factor = (w * h) / (W * H)
                    score = 0.65 * (1.0 + min(1.0, size_factor * 100))
                    all_dets.append((real_x, int(y), int(w), int(h), float(score)))
            except Exception:
                pass

        # === NMS to merge duplicate detections from different cascades/scales ===
        merged = _nms(all_dets, iou_threshold=0.3)

        # === Skin-tone filter: reject false positives in backgrounds ===
        # Fail open: if skin filter rejects EVERYTHING, fall back to unfiltered
        # (better to occasionally false-positive than to miss real faces in
        # animated/stylized content)
        skin_filtered = self._filter_by_skin_tone(merged, frame_bgr)
        if skin_filtered or not merged:
            return skin_filtered
        return merged  # fallback when skin filter is too aggressive

    def _filter_by_skin_tone(self, detections, frame_bgr) -> list[tuple]:
        """
        Reject detections whose bounding box contains <15% skin-tone pixels.
        Skin tones span a wide range (Y'CbCr is the most stable color space for
        this); we use a generous range that covers most ethnicities/lighting.
        """
        if not detections:
            return []
        try:
            cv2 = self.cv2
            ycrcb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2YCrCb)
            # Y'CbCr skin range — very inclusive, validated across datasets
            lower = (0, 133, 77)
            upper = (255, 173, 127)
            skin_mask = cv2.inRange(ycrcb, lower, upper)
        except Exception:
            return detections  # fail open: don't lose detections on filter error

        kept = []
        for det in detections:
            x, y, w, h, score = det
            x = max(0, x); y = max(0, y)
            w = min(skin_mask.shape[1] - x, w)
            h = min(skin_mask.shape[0] - y, h)
            if w <= 0 or h <= 0:
                continue
            patch = skin_mask[y:y+h, x:x+w]
            if patch.size == 0:
                continue
            skin_ratio = float((patch > 0).sum()) / patch.size
            # 15% threshold — generous enough to keep faces with hair/glasses/beard
            # but reject random patches in backgrounds and logos
            if skin_ratio >= 0.15:
                # Boost score slightly for high-confidence skin matches
                bonus = 1.0 + min(0.3, skin_ratio - 0.15)
                kept.append((x, y, w, h, score * bonus))
        return kept


def _nms(detections: list[tuple], iou_threshold: float = 0.3) -> list[tuple]:
    """Non-max suppression: merge overlapping detections from different cascades."""
    if not detections:
        return []
    dets = sorted(detections, key=lambda d: d[4], reverse=True)
    keep = []
    while dets:
        best = dets.pop(0)
        keep.append(best)
        bx1, by1, bw, bh, _ = best
        bx2, by2 = bx1 + bw, by1 + bh
        remaining = []
        for d in dets:
            x1, y1, w, h, _ = d
            x2, y2 = x1 + w, y1 + h
            ix1, iy1 = max(bx1, x1), max(by1, y1)
            ix2, iy2 = min(bx2, x2), min(by2, y2)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            union = bw * bh + w * h - inter
            iou = (inter / union) if union > 0 else 0.0
            if iou < iou_threshold:
                remaining.append(d)
        dets = remaining
    return keep


def _cluster_speakers(
    per_frame_detections: list[list[tuple]],
    source_w: int,
    dist_threshold_pct: float = 0.15,
) -> list[dict]:
    """
    Cluster face detections across frames into 'speakers' — same person
    appearing in roughly the same position across multiple frames.
    Returns list of speakers (most-seen first).
    """
    threshold_px = source_w * dist_threshold_pct
    clusters: list[dict] = []

    for frame_idx, dets in enumerate(per_frame_detections):
        for x, y, w, h, score in dets:
            cx = x + w // 2
            cy = y + h // 2
            best_c = None
            best_dist = float('inf')
            for c in clusters:
                cluster_cx = c['x_sum'] / c['count']
                d = abs(cx - cluster_cx)
                if d < threshold_px and d < best_dist:
                    best_dist = d
                    best_c = c
            if best_c:
                best_c['x_sum'] += cx
                best_c['y_sum'] += cy
                best_c['w_sum'] += w
                best_c['h_sum'] += h
                best_c['count'] += 1
                best_c['score_sum'] += score
                best_c['frames'].add(frame_idx)
            else:
                clusters.append({
                    'x_sum': cx, 'y_sum': cy, 'w_sum': w, 'h_sum': h,
                    'count': 1, 'score_sum': float(score),
                    'frames': {frame_idx},
                })

    speakers = []
    for c in clusters:
        speakers.append({
            'avg_x': c['x_sum'] / c['count'],
            'avg_y': c['y_sum'] / c['count'],
            'avg_w': c['w_sum'] / c['count'],
            'avg_h': c['h_sum'] / c['count'],
            'frame_count': len(c['frames']),
            'detection_count': c['count'],
            'score_sum': c['score_sum'],
        })
    speakers.sort(
        key=lambda s: (-s['frame_count'], -s['score_sum'], -s['avg_w'] * s['avg_h'])
    )
    return speakers


def _extract_frame_bgr(
    source_video: Path, timestamp_sec: float,
    source_w: int, source_h: int,
):
    """Extract a single frame at timestamp as BGR numpy array. None on failure."""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{timestamp_sec:.3f}",
            "-i", str(source_video),
            "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        if result.returncode != 0 or not result.stdout:
            return None
        expected = source_w * source_h * 3
        if len(result.stdout) < expected:
            return None
        arr = np.frombuffer(result.stdout[:expected], dtype=np.uint8)
        return arr.reshape((source_h, source_w, 3))
    except Exception:
        return None


# =============================================================================
# Public API
# =============================================================================
def find_smart_crop_x(
    source_video: Path,
    clip_start: float,
    clip_duration: float,
    source_width: int,
    source_height: int,
    target_aspect: float,
    fallback_position: str = "center",
) -> Optional[float]:
    """
    Compute the optimal horizontal crop center using multi-cascade ensemble +
    multi-frame voting. Returns the X coordinate (in source-pixel space) of
    the primary speaker, or None if detection failed.
    """
    source_aspect = source_width / source_height
    if source_aspect <= target_aspect + 0.01:
        return None

    target_crop_w = int(source_height * target_aspect)
    if target_crop_w >= source_width:
        return None

    sample_times = []
    if clip_duration <= 1.0:
        sample_times = [clip_start + clip_duration / 2]
    else:
        for i in range(_SAMPLE_FRAME_COUNT):
            t = clip_start + (i + 0.5) * clip_duration / _SAMPLE_FRAME_COUNT
            sample_times.append(t)

    detector = _FaceDetector()
    detector._ensure_loaded()
    if not detector.cascades:
        logger.warning("Smart crop: no cascades loaded — falling back")
        return None

    min_face_size = max(40, int(source_width * _MIN_FACE_FRAC))
    per_frame_detections: list[list[tuple]] = []

    for t in sample_times:
        frame = _extract_frame_bgr(source_video, t, source_width, source_height)
        if frame is None:
            per_frame_detections.append([])
            continue
        dets = detector.detect_in_frame(frame, min_face_size=min_face_size)
        dets = [d for d in dets if d[1] >= source_height * 0.05]
        per_frame_detections.append(dets)

    total = sum(len(dets) for dets in per_frame_detections)
    if total == 0:
        logger.info(
            f"Smart crop: no faces detected in {len(sample_times)} samples — "
            f"using fallback crop_position={fallback_position}"
        )
        return None

    speakers = _cluster_speakers(per_frame_detections, source_width)
    if not speakers:
        return None

    primary = speakers[0]
    half_crop_w = target_crop_w / 2
    crop_center = max(half_crop_w,
                      min(source_width - half_crop_w, primary['avg_x']))

    logger.info(
        f"Smart crop: detected {len(speakers)} speaker(s), "
        f"primary at x={primary['avg_x']:.0f} "
        f"(seen in {primary['frame_count']}/{len(sample_times)} frames) "
        f"→ crop center = {crop_center:.0f}"
    )
    return crop_center


def detect_speakers_for_keyframes(
    source_video: Path,
    clip_start: float,
    clip_duration: float,
    source_width: int,
    source_height: int,
    max_speakers: int = 3,
) -> list[dict]:
    """
    Detect distinct speakers in the clip and return their normalized positions.
    Used by the reframe modal to suggest auto-keyframe positions.

    Returns: [{x_pct, y_pct, frame_count}, ...] sorted by importance.
    """
    detector = _FaceDetector()
    detector._ensure_loaded()
    if not detector.cascades:
        return []

    sample_times = []
    if clip_duration <= 1.0:
        sample_times = [clip_start + clip_duration / 2]
    else:
        for i in range(_SAMPLE_FRAME_COUNT):
            t = clip_start + (i + 0.5) * clip_duration / _SAMPLE_FRAME_COUNT
            sample_times.append(t)

    min_face_size = max(40, int(source_width * _MIN_FACE_FRAC))
    per_frame: list[list[tuple]] = []
    for t in sample_times:
        frame = _extract_frame_bgr(source_video, t, source_width, source_height)
        if frame is None:
            per_frame.append([])
            continue
        dets = detector.detect_in_frame(frame, min_face_size=min_face_size)
        dets = [d for d in dets if d[1] >= source_height * 0.05]
        per_frame.append(dets)

    speakers = _cluster_speakers(per_frame, source_width)
    out = []
    for s in speakers[:max_speakers]:
        # Include face bbox as percentage of source — frontend uses this to
        # warn about edge-clipping or auto-zoom into tight shots
        out.append({
            "x_pct": float(s['avg_x']) / source_width,
            "y_pct": float(s['avg_y']) / source_height,
            "w_pct": float(s['avg_w']) / source_width,
            "h_pct": float(s['avg_h']) / source_height,
            "frame_count": s['frame_count'],
        })
    return out


def auto_keyframes_from_detection(
    source_video: Path,
    clip_start: float,
    clip_duration: float,
    source_width: int,
    source_height: int,
) -> list[dict]:
    """
    Generate keyframes spread across the clip timeline using per-time speaker
    detection. Used by the "Auto-fit speakers" one-click button.

    Strategy (V2 — preserves speaker positions across the whole video):
      1. Sample 8-16 frames densely across the clip
      2. For each frame, identify the LARGEST visible face (active speaker)
      3. CLUSTER all detections into 2-3 "speaker positions" using K-means-style
         grouping on x_pct (since podcasts/interviews are usually horizontal)
      4. SNAP each frame's keyframe to its cluster's representative position
         (so left speaker is ALWAYS at exactly the same x_pct, no wobble)
      5. Compress consecutive frames pointing to the same cluster
      6. Enforce minimum 1.5s dwell between keyframes (drop noise)
      7. Force first keyframe at t=0

    Returns: [{t, x_pct, y_pct}, ...] (clip-relative t in seconds)
    """
    detector = _FaceDetector()
    detector._ensure_loaded()
    if not detector.cascades:
        return []

    n_samples = max(8, min(16, int(clip_duration * 1.5)))
    sample_times = [
        clip_start + (i + 0.5) * clip_duration / n_samples
        for i in range(n_samples)
    ]

    min_face_size = max(40, int(source_width * _MIN_FACE_FRAC))
    raw_kfs: list[dict] = []

    for t in sample_times:
        frame = _extract_frame_bgr(source_video, t, source_width, source_height)
        if frame is None:
            continue
        dets = detector.detect_in_frame(frame, min_face_size=min_face_size)
        dets = [d for d in dets if d[1] >= source_height * 0.05]
        if not dets:
            continue
        dets.sort(key=lambda d: d[2] * d[3], reverse=True)
        x, y, w, h, _ = dets[0]
        raw_kfs.append({
            "t": float(t - clip_start),
            "x_pct": float(x + w / 2) / source_width,
            "y_pct": float(y + h / 2) / source_height,
        })

    if not raw_kfs:
        return []

    # === STEP 1: Cluster x positions into discrete "speaker slots" ===
    # Use a simple greedy 1D clustering: any two detections within 15% of source
    # width belong to the same speaker. After this, each cluster has a single
    # canonical (x_pct, y_pct) — its centroid — that ALL its keyframes will use.
    CLUSTER_THRESHOLD = 0.15
    clusters: list[dict] = []
    for kf in raw_kfs:
        # Find the closest existing cluster
        best = None
        best_dist = float('inf')
        for c in clusters:
            d = abs(kf["x_pct"] - c["x_sum"] / c["count"])
            if d < CLUSTER_THRESHOLD and d < best_dist:
                best = c
                best_dist = d
        if best is not None:
            best["x_sum"] += kf["x_pct"]
            best["y_sum"] += kf["y_pct"]
            best["count"] += 1
            kf["_cluster"] = id(best)
        else:
            new_c = {"x_sum": kf["x_pct"], "y_sum": kf["y_pct"], "count": 1}
            clusters.append(new_c)
            kf["_cluster"] = id(new_c)

    # Build cluster id → centroid map (the canonical position for that speaker)
    centroids = {
        id(c): (c["x_sum"] / c["count"], c["y_sum"] / c["count"])
        for c in clusters
    }

    # === STEP 2: Snap every raw keyframe to its cluster's centroid ===
    snapped: list[dict] = []
    for kf in raw_kfs:
        cx, cy = centroids[kf["_cluster"]]
        snapped.append({
            "t": kf["t"],
            "x_pct": cx,
            "y_pct": cy,
            "_cluster": kf["_cluster"],
        })

    # === STEP 3: Compress consecutive same-cluster keyframes ===
    compressed: list[dict] = [snapped[0]]
    for kf in snapped[1:]:
        if kf["_cluster"] == compressed[-1]["_cluster"]:
            continue  # same speaker, skip
        compressed.append(kf)

    # === STEP 4: Enforce minimum dwell time between keyframes ===
    # If two cluster-switches happen within 1.5 seconds, drop the second —
    # this is detection noise (e.g. brief occlusion, head turn), not a real
    # speaker switch. Camera shouldn't flick.
    MIN_DWELL_SEC = 1.5
    dwelled: list[dict] = [compressed[0]]
    for kf in compressed[1:]:
        if (kf["t"] - dwelled[-1]["t"]) < MIN_DWELL_SEC:
            continue
        dwelled.append(kf)

    # === STEP 5: Force first keyframe at t=0 ===
    if dwelled[0]["t"] > 0.001:
        dwelled[0] = {**dwelled[0], "t": 0.0}

    # Strip internal _cluster field from output
    return [
        {"t": kf["t"], "x_pct": kf["x_pct"], "y_pct": kf["y_pct"]}
        for kf in dwelled
    ]


def build_smart_crop_filter(
    source_video: Path,
    clip_start: float,
    clip_duration: float,
    source_width: int,
    source_height: int,
    out_w: int,
    out_h: int,
    fallback_position: str = "center",
    user_crop: dict | None = None,
) -> str:
    """
    Build a complete scale+crop filter string for FFmpeg.

    Priority: user_crop > face-detected > fallback_position.
    user_crop format: v1 {x_pct, y_pct, zoom} OR v2 {version: 2, keyframes, ...}
    """
    # === Priority 1: user override (v1 or v2) ===
    if user_crop is not None:
        try:
            from backend.pipeline.reframe import (
                build_keyframe_crop_filter, build_precise_crop_filter,
                normalize_user_crop,
            )
            norm = normalize_user_crop(user_crop)
            if norm is None:
                logger.warning("Smart crop: user_crop invalid, falling through to auto")
            else:
                kfs = norm["keyframes"]
                if len(kfs) == 1:
                    kf = kfs[0]
                    filt = build_precise_crop_filter(
                        source_w=source_width, source_h=source_height,
                        out_w=out_w, out_h=out_h,
                        x_pct=kf["x_pct"], y_pct=kf["y_pct"],
                        zoom=norm.get("zoom", 1.0),
                    )
                    logger.info(
                        f"Smart crop: USER OVERRIDE (single keyframe) "
                        f"x_pct={kf['x_pct']:.3f}, y_pct={kf['y_pct']:.3f}"
                    )
                    return filt
                else:
                    filt = build_keyframe_crop_filter(
                        source_w=source_width, source_h=source_height,
                        out_w=out_w, out_h=out_h,
                        user_crop=norm,
                    )
                    logger.info(
                        f"Smart crop: USER OVERRIDE ({len(kfs)} keyframes, "
                        f"transition={norm.get('transition')})"
                    )
                    return filt
        except Exception as e:
            logger.warning(f"Smart crop: user_crop processing failed ({e}), falling through")

    # === Priority 2: face-detected smart crop ===
    target_aspect = out_w / out_h
    smart_x_center = find_smart_crop_x(
        source_video=source_video,
        clip_start=clip_start, clip_duration=clip_duration,
        source_width=source_width, source_height=source_height,
        target_aspect=target_aspect, fallback_position=fallback_position,
    )

    scale = (
        f"scale=w='if(gt(a,{out_w}/{out_h}),-2,{out_w})':"
        f"h='if(gt(a,{out_w}/{out_h}),{out_h},-2)'"
    )

    # === Priority 3: fallback_position ===
    if smart_x_center is None:
        if fallback_position == "top":
            crop_x, crop_y = "(iw-ow)/2", "0"
        elif fallback_position == "bottom":
            crop_x, crop_y = "(iw-ow)/2", "ih-oh"
        elif fallback_position == "left":
            crop_x, crop_y = "0", "(ih-oh)/2"
        elif fallback_position == "right":
            crop_x, crop_y = "iw-ow", "(ih-oh)/2"
        else:
            crop_x, crop_y = "(iw-ow)/2", "(ih-oh)/2"
        return f"{scale},crop={out_w}:{out_h}:{crop_x}:{crop_y}"

    scale_factor = out_h / source_height
    scaled_face_center_x = smart_x_center * scale_factor
    desired_crop_x = scaled_face_center_x - out_w / 2
    scaled_w = source_width * scale_factor
    desired_crop_x = max(0.0, min(scaled_w - out_w, desired_crop_x))
    desired_crop_x_int = int(desired_crop_x)

    return f"{scale},crop={out_w}:{out_h}:{desired_crop_x_int}:(ih-oh)/2"


def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Probe video for (width, height). Returns (0, 0) on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(video_path)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        w_str, h_str = result.stdout.strip().split("x")
        return int(w_str), int(h_str)
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        return (0, 0)