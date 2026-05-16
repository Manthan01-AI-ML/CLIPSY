"""
scripts/test_face_detection.py

Phase 2A.1 visual verification — cascading MediaPipe face detection.

Changes from Phase 2A:
  - Calls detect_faces_with_retry() (returns faces + detection path)
  - 8 frames per video at [5, 15, 25, 40, 55, 70, 85, 95]% — catches cutaways
  - Detection path annotated on bottom-left of each JPEG (colour-coded)
  - When path is "clahe+full": saves before/after CLAHE comparison JPEGs
  - summary.json includes per-frame detection_path

Run from inside the backend container:
    docker exec clipwise-backend-1 python /tmp/test_face_detection.py

Outputs (up to 8 annotated frames per video + CLAHE pairs where triggered):
  debug_output/{stem}_{pct:02d}pct.jpg
  debug_output/{stem}_{pct:02d}pct_clahe_before.jpg   (only when CLAHE ran)
  debug_output/{stem}_{pct:02d}pct_clahe_after.jpg    (only when CLAHE ran)
  debug_output/summary.json

Path label colours (bottom-left of each image):
  green   = short        (short-range model, large close-up face)
  orange  = full         (full-range model, group/panel/wide)
  magenta = clahe+full   (CLAHE preprocessing + full-range)
  red     = none         (no face detected on any pass)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_face_detection")

# ---------------------------------------------------------------------------
# Paths — container takes priority; host paths used for local debug runs
# ---------------------------------------------------------------------------
_CONTAINER_VIDEO_DIR = Path("/app/test_videos")
_CONTAINER_DEBUG_DIR = Path("/app/scripts/debug_output")
_HOST_VIDEO_DIR = Path("C:/Resume Projects/Clipwise/test_videos")
_HOST_DEBUG_DIR = Path("C:/Resume Projects/Clipwise/clipwise/scripts/debug_output")

if _CONTAINER_VIDEO_DIR.exists():
    VIDEO_DIR = _CONTAINER_VIDEO_DIR
    DEBUG_DIR = _CONTAINER_DEBUG_DIR
else:
    VIDEO_DIR = _HOST_VIDEO_DIR
    DEBUG_DIR = _HOST_DEBUG_DIR

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
SAMPLE_PCTS = [5, 15, 25, 40, 55, 70, 85, 95]  # 8 frames — catches cutaways + speaker frames

# Bounding box / label colour per detection path (BGR)
_PATH_COLOR = {
    "short":      (0, 220, 0),    # green
    "full":       (0, 165, 255),  # orange
    "clahe+full": (255, 0, 200),  # magenta
    "none":       (0, 0, 255),    # red
}


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def _get_video_duration(video_path: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[:200]}")
    return float(result.stdout.strip())


def _get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Return (width, height) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe dims failed: {result.stderr.strip()[:200]}")
    w_str, h_str = result.stdout.strip().split("x")
    return int(w_str), int(h_str)


def _extract_frame(
    video_path: Path, timestamp_sec: float, W: int, H: int
) -> np.ndarray | None:
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
        logger.warning(f"Frame extraction timed out at {timestamp_sec:.1f}s")
        return None

    expected = W * H * 3
    if result.returncode != 0 or len(result.stdout) < expected:
        return None

    arr = np.frombuffer(result.stdout[:expected], dtype=np.uint8)
    return arr.reshape((H, W, 3))


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

def _annotate_frame(
    frame: np.ndarray,
    faces: list[dict],
    detection_path: str,
) -> np.ndarray:
    """Draw bboxes, confidence labels, landmarks, and detection-path badge."""
    import cv2

    out = frame.copy()
    box_color = _PATH_COLOR.get(detection_path, (128, 128, 128))

    for face in faces:
        x, y, w, h = face["bbox"]
        conf = face["confidence"]

        # Bounding box — colour matches detection path
        cv2.rectangle(out, (x, y), (x + w, y + h), box_color, 2)

        # Confidence label on dark pill
        label = f"{conf:.2f}"
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale, thick = 0.55, 1
        (lw, lh), bl = cv2.getTextSize(label, font, scale, thick)
        pad = 3
        cv2.rectangle(out, (x, max(0, y - lh - bl - pad * 2)),
                      (x + lw + pad * 2, y), (0, 0, 0), -1)
        cv2.putText(out, label, (x + pad, max(lh, y - bl - pad)),
                    font, scale, (255, 255, 255), thick, cv2.LINE_AA)

        # Landmarks — coral circles
        for lx, ly in face.get("landmarks", []):
            cv2.circle(out, (lx, ly), 3, (80, 95, 255), -1)

    if not faces:
        cv2.putText(out, "NO FACE DETECTED",
                    (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 2, cv2.LINE_AA)

    # Detection-path badge — bottom-left corner
    H_fr, W_fr = out.shape[:2]
    badge = f"path: {detection_path}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thick = 0.65, 2
    (lw, lh), bl = cv2.getTextSize(badge, font, scale, thick)
    pad = 6
    bx1, by2 = 10, H_fr - 10
    bx2, by1 = bx1 + lw + pad * 2, by2 - lh - bl - pad * 2
    cv2.rectangle(out, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
    cv2.putText(out, badge, (bx1 + pad, by2 - bl - pad),
                font, scale, box_color, thick, cv2.LINE_AA)

    return out


def _save_jpeg(path: Path, frame: np.ndarray) -> bool:
    import cv2
    ok = cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        logger.error(f"Failed to write {path}")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure /app is on sys.path when running as a copied script in /tmp/
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")

    try:
        from backend.pipeline.face_detection import (
            detect_faces_with_retry,
            enhance_for_detection,
        )
    except ImportError as e:
        logger.error(f"Cannot import face_detection: {e}")
        logger.error("Run inside the backend container.")
        sys.exit(1)

    try:
        import cv2  # noqa: F401
    except ImportError:
        logger.error("OpenCV (cv2) not available — check requirements.txt.")
        sys.exit(1)

    if not VIDEO_DIR.exists():
        logger.error(f"Video directory not found: {VIDEO_DIR}")
        sys.exit(1)

    videos = sorted(
        p for p in VIDEO_DIR.iterdir()
        if p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not videos:
        logger.error(f"No video files in {VIDEO_DIR}")
        sys.exit(1)

    logger.info(
        f"Found {len(videos)} video(s)  |  "
        f"{len(SAMPLE_PCTS)} frames each  |  "
        f"output → {DEBUG_DIR}"
    )

    summary: dict[str, dict] = {}

    for video_path in videos:
        stem = video_path.stem
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {video_path.name}")

        try:
            duration = _get_video_duration(video_path)
            W, H = _get_video_dimensions(video_path)
        except Exception as e:
            logger.error(f"  Cannot probe {video_path.name}: {e}")
            summary[stem] = {"error": str(e)}
            continue

        logger.info(f"  Duration: {duration:.1f}s  |  {W}x{H}")

        video_stats: dict = {
            "duration_sec": round(duration, 2),
            "dimensions": f"{W}x{H}",
            "frames": [],
            "path_counts": {"short": 0, "full": 0, "clahe+full": 0, "none": 0},
            "total_faces_detected": 0,
            "output_files": [],
        }

        for pct in SAMPLE_PCTS:
            ts = min(duration * (pct / 100.0), max(0.0, duration - 0.5))
            frame = _extract_frame(video_path, ts, W, H)
            if frame is None:
                logger.warning(f"  [{pct:3d}%] extraction failed at {ts:.1f}s")
                continue

            faces, path = detect_faces_with_retry(frame)
            n = len(faces)
            confs = [round(f["confidence"], 3) for f in faces]

            logger.info(
                f"  [{pct:3d}%  t={ts:6.1f}s]  {n} face(s)  path={path}"
                + (f"  conf={confs}" if n else "")
            )

            video_stats["total_faces_detected"] += n
            video_stats["path_counts"][path] = (
                video_stats["path_counts"].get(path, 0) + 1
            )

            frame_record: dict = {
                "pct": pct,
                "timestamp_sec": round(ts, 2),
                "detection_path": path,
                "faces_found": n,
                "confidences": confs,
            }

            # Annotated main image
            annotated = _annotate_frame(frame, faces, path)
            out_name = f"{stem}_{pct:02d}pct.jpg"
            if _save_jpeg(DEBUG_DIR / out_name, annotated):
                video_stats["output_files"].append(out_name)
                frame_record["output_file"] = out_name
                logger.info(f"           → {out_name}")

            # CLAHE before/after comparison (only when that path was taken)
            if path == "clahe+full":
                enhanced = enhance_for_detection(frame)
                before_name = f"{stem}_{pct:02d}pct_clahe_before.jpg"
                after_name  = f"{stem}_{pct:02d}pct_clahe_after.jpg"

                if _save_jpeg(DEBUG_DIR / before_name, frame):
                    video_stats["output_files"].append(before_name)
                    frame_record["clahe_before"] = before_name
                    logger.info(f"           → {before_name}  [CLAHE before]")

                if _save_jpeg(DEBUG_DIR / after_name, enhanced):
                    video_stats["output_files"].append(after_name)
                    frame_record["clahe_after"] = after_name
                    logger.info(f"           → {after_name}  [CLAHE after]")

            video_stats["frames"].append(frame_record)

        summary[stem] = video_stats

    # Write summary.json
    summary_path = DEBUG_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"\n{'='*60}")
    logger.info(f"Summary → {summary_path}")

    # Human-readable report
    logger.info("\n=== DETECTION REPORT ===")
    total_jpegs = 0
    for vid, stats in summary.items():
        if "error" in stats:
            logger.info(f"  {vid}: ERROR — {stats['error']}")
            continue
        n_frames = len(stats.get("frames", []))
        n_out = len(stats.get("output_files", []))
        total_jpegs += n_out
        paths = stats.get("path_counts", {})
        logger.info(
            f"  {vid}: {n_frames}/{len(SAMPLE_PCTS)} frames  "
            f"faces={stats['total_faces_detected']}  "
            f"paths={paths}  "
            f"files={n_out}"
        )

    logger.info(f"\nTotal output files: {total_jpegs} JPEGs + 1 summary.json")
    logger.info("Phase 2A.1 visual verification complete — inspect debug_output/.")


if __name__ == "__main__":
    main()
