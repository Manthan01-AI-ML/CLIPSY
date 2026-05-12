"""
backend/services/storage.py

File storage abstraction + magic-byte validation (Step 11).

Security model:
  - Each user's files isolated in /storage/<user_id>/
  - Path sanitization via os.path.abspath (prevents traversal)
  - Extension whitelist for cheap first-pass rejection
  - Magic-byte validation (python-magic) for actual content type
    → prevents disguised malware (e.g. .exe renamed to .mp4)
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from backend.core.config import settings

logger = logging.getLogger(__name__)


# Accepted video extensions (cheap first filter)
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

# Accepted MIME types (real check via magic bytes)
# python-magic returns specific mime strings — we allow any video/* plus
# common container-specific ones that magic doesn't always recognize as video/*
ALLOWED_VIDEO_MIMES = {
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
    "video/x-msvideo",
    "video/avi",
    "video/x-flv",
    # Some MP4 files are detected as these (legitimately)
    "application/mp4",
    "application/octet-stream",  # falls through to extension check (see validate_video_bytes)
}


def _user_dir(user_id: uuid.UUID | str, subdir: str) -> Path:
    """
    Return absolute path to a user's subdirectory, creating if needed.
    Path-traversal hardened.
    """
    base = Path(settings.STORAGE_PATH).resolve()
    path = base / str(user_id) / subdir

    resolved = path.resolve()
    if not str(resolved).startswith(str(base)):
        raise ValueError(f"Refusing to create path outside storage root: {resolved}")

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def upload_path(user_id: uuid.UUID, job_id: uuid.UUID, original_filename: str) -> Path:
    """
    Compute where an uploaded raw video should be saved.
    Format: /storage/<user_id>/raw/<job_id>.<ext>

    NOTE: This only reserves the path — it does NOT validate content bytes.
    Call validate_video_bytes() after writing to check real type.
    """
    ext = Path(original_filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError(
            f"File extension '{ext}' not allowed. "
            f"Accepted: {sorted(ALLOWED_VIDEO_EXTENSIONS)}"
        )
    return _user_dir(user_id, "raw") / f"{job_id}{ext}"


def clip_output_dir(user_id: uuid.UUID, job_id: uuid.UUID) -> Path:
    return _user_dir(user_id, f"clips/{job_id}")


def audio_path(user_id: uuid.UUID, job_id: uuid.UUID) -> Path:
    return _user_dir(user_id, "audio") / f"{job_id}.wav"


def ensure_storage_ready() -> None:
    """Called once at app startup. Confirms STORAGE_PATH is writable."""
    base = Path(settings.STORAGE_PATH)
    base.mkdir(parents=True, exist_ok=True)
    test_file = base / ".write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
    except OSError as e:
        raise RuntimeError(f"Storage path not writable: {base} ({e})")


# =============================================================================
# MAGIC-BYTE VALIDATION (Step 11)
# =============================================================================
# Why: Extensions are trivially faked. A malicious .mp4 could actually be an
# .exe with a renamed extension. python-magic reads the actual file bytes.
#
# Defense-in-depth:
#   Layer 1: Extension whitelist (cheap, stops lazy attacks)
#   Layer 2: Magic-byte check (real, stops content-type spoofing)
#   Layer 3: FFmpeg probe fails gracefully downstream if content is actually
#            corrupt or not a real video

def validate_video_bytes(file_path: Path) -> None:
    """
    Check file's actual MIME type via magic bytes.
    Raises ValueError if the file's content doesn't look like a video.

    Must be called AFTER the file has been fully written to disk.
    """
    if not file_path.exists():
        raise ValueError(f"File not found: {file_path}")

    if file_path.stat().st_size == 0:
        raise ValueError("File is empty")

    try:
        import magic  # python-magic
    except ImportError:
        logger.warning(
            "python-magic not installed. Skipping magic-byte validation. "
            "Install libmagic1 + pip install python-magic to enable."
        )
        return

    try:
        # mime=True returns e.g. "video/mp4"
        detector = magic.Magic(mime=True)
        detected_mime = detector.from_file(str(file_path))
    except Exception as e:
        # magic library itself failed — don't fail the upload over this,
        # but log loudly so ops knows
        logger.error(f"Magic-byte detection failed for {file_path.name}: {e}")
        return

    logger.info(f"Magic-byte check: {file_path.name} → {detected_mime}")

    # Normalize: "video/x-matroska; charset=binary" → "video/x-matroska"
    mime_clean = detected_mime.split(";")[0].strip().lower()

    # Accept video/* category OR specific known-safe types
    if mime_clean.startswith("video/"):
        return

    # Some valid MP4s are detected as application/mp4 (spec-compliant)
    if mime_clean in ALLOWED_VIDEO_MIMES:
        return

    # application/octet-stream = "we don't know" — fall back to extension check
    # This is lenient but not dangerous: we've already checked extension at route.
    # A file that's truly an .exe would get a DIFFERENT mime (application/x-dosexec)
    if mime_clean == "application/octet-stream":
        logger.warning(
            f"File {file_path.name} detected as octet-stream. "
            f"Proceeding based on extension only."
        )
        return

    # Anything else is rejected
    raise ValueError(
        f"File content does not look like a video. "
        f"Detected type: '{mime_clean}'. "
        f"Please upload an actual video file (mp4, mov, mkv, webm, avi)."
    )