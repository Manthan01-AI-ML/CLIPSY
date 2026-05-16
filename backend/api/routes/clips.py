"""backend/api/routes/clips.py — Step 13: editable transcripts + re-render endpoint.
Session B2: + canvas customization (meme-style text/thumbnail overlay)."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.api.deps import get_current_user
from backend.core.database import get_db
from backend.models.clip import Clip
from backend.models.user import User
from backend.schemas.clip import (
    ClipOut, ClipListOut,
    TranscriptUpdateRequest, RerenderResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _rebuild_clip_from_source(clip: Clip, db: Session) -> None:
    """
    Re-render a SINGLE clip from its parent job's source video.

    HARDENING (v8): defensive flow that GUARANTEES user_crop is applied.
    Three paths based on what's patched:

      A1 — rerender_clip_with_edits accepts user_crop kwarg (fully patched):
           pass it through directly. Cleanest.

      A2 — rerender_clip_with_edits doesn't accept user_crop, but render_one_clip
           does (Patches 1+2 applied, Patch 3 missing):
           BYPASS rerender_clip_with_edits and call render_one_clip directly
           with user_crop. This is the user's most common state per their
           patcher output. ✓ The fix the user has been waiting for.

      A3 — neither function accepts user_crop (no patches applied):
           call rerender as-is, log loud warning, set _reframe_warning on
           clip.meta so frontend can confirm dialog the user.

    Synchronous: blocks 20-60 seconds. The user explicitly clicked Reset
    or Save in reframe modal and is waiting for it.

    Mutates `clip.file_path` and `clip.thumbnail_path` in the DB.
    """
    import inspect
    from backend.models.video import VideoJob
    from backend.pipeline.render import rerender_clip_with_edits, render_one_clip
    from backend.services.storage import clip_output_dir, _user_dir
    from backend.core.config import settings

    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None:
        raise RuntimeError("parent job not found — cannot rebuild from source")

    job_meta = job.meta or {}
    aspect_ratio = job_meta.get("aspect_ratio", "9:16")
    crop_position = job_meta.get("crop_position", "center")
    caption_preset = job_meta.get("caption_preset", settings.DEFAULT_CAPTION_PRESET)
    detected_language = job_meta.get("language", "en")

    source_path = job.file_path
    if not source_path:
        raw_dir = _user_dir(clip.user_id, "raw")
        for ext in (".mp4", ".webm", ".mkv", ".mov"):
            candidate = raw_dir / f"{job.id}{ext}"
            if candidate.exists():
                source_path = str(candidate)
                job.file_path = source_path
                db.commit()
                break

    if not source_path or not Path(source_path).exists():
        raise RuntimeError(f"source video missing: {source_path}")

    output_dir = clip_output_dir(clip.user_id, clip.video_job_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not clip.edited_transcript and clip.transcript:
        clip.edited_transcript = list(clip.transcript)
        flag_modified(clip, "edited_transcript")
        db.commit()
        logger.info(
            f"[clip {clip.id}] populated edited_transcript from transcript "
            f"({len(clip.edited_transcript)} segments) for rebuild"
        )

    # Pull user_crop from clip.meta (set by reframe modal save)
    user_crop_from_meta = (clip.meta or {}).get("user_crop")

    # === DETECT PATCHER STATUS ===
    sig = inspect.signature(render_one_clip)
    render_accepts_user_crop = "user_crop" in sig.parameters

    rerender_sig = inspect.signature(rerender_clip_with_edits)
    rerender_accepts_user_crop = "user_crop" in rerender_sig.parameters

    if user_crop_from_meta is not None:
        logger.info(
            f"[clip {clip.id}] reframe rebuild — user_crop present, "
            f"render_one_clip accepts user_crop={render_accepts_user_crop}, "
            f"rerender_clip_with_edits accepts user_crop={rerender_accepts_user_crop}"
        )

    # === RENDER — choose path based on what's patched ===
    new_clip_path = None
    new_thumb_path = None
    used_direct_render_one_clip = False  # for debug + warning suppression

    if rerender_accepts_user_crop:
        # === PATH A1: full rerender with user_crop forwarded ===
        new_clip_path, new_thumb_path = rerender_clip_with_edits(
            source=Path(source_path),
            clip_row=clip,
            output_dir=output_dir,
            job_id=job.id,
            aspect_ratio=aspect_ratio,
            crop_position=crop_position,
            caption_preset=caption_preset,
            detected_language=detected_language,
            user_crop=user_crop_from_meta,
        )
        logger.info(f"[clip {clip.id}] rendered via path A1 (rerender + user_crop kwarg)")

    elif render_accepts_user_crop and user_crop_from_meta is not None:
        # === PATH A2: BYPASS rerender_clip_with_edits — call render_one_clip directly ===
        # This is the user's case (per patcher output): render_one_clip is patched
        # but rerender_clip_with_edits is not. We replicate what rerender does
        # (build clip dict, use edited_transcript) and call render_one_clip with
        # user_crop directly.
        clip_dict = {
            "rank": int(clip.rank or 1),
            "start": float(clip.start_sec or 0),
            "duration": float(
                clip.duration_sec or
                (float(clip.end_sec or 0) - float(clip.start_sec or 0)) or
                30
            ),
            # Add fields render_one_clip might inspect — be generous
            "title": clip.title or "",
            "hook": clip.hook or "",
        }
        # Use edited_transcript (already populated above if it was empty)
        transcript_segments = clip.edited_transcript or clip.transcript or []

        # Build kwargs dynamically — only pass what render_one_clip accepts
        # (in case the user's render_one_clip signature is slightly different
        # from canonical)
        params = sig.parameters
        kwargs = {
            "source": Path(source_path),
            "clip": clip_dict,
            "transcript_segments": transcript_segments,
            "output_dir": output_dir,
            "job_id": job.id,
        }
        # Optional kwargs — only pass if render_one_clip accepts them
        for opt_name, opt_value in [
            ("aspect_ratio", aspect_ratio),
            ("crop_position", crop_position),
            ("caption_preset", caption_preset),
            ("detected_language", detected_language),
            ("user_crop", user_crop_from_meta),
        ]:
            if opt_name in params:
                kwargs[opt_name] = opt_value

        try:
            result = render_one_clip(**kwargs)
            # render_one_clip returns (clip_file, thumb_file)
            if isinstance(result, tuple) and len(result) == 2:
                new_clip_path, new_thumb_path = result
            else:
                new_clip_path = result
                new_thumb_path = None
            used_direct_render_one_clip = True
            logger.info(
                f"[clip {clip.id}] rendered via path A2 "
                f"(direct render_one_clip with user_crop, bypassing rerender_clip_with_edits) — "
                f"this is the path the user's patched-render-but-no-patch-3 needs"
            )
        except TypeError as e:
            # Signature mismatch — fall through to A3
            logger.warning(
                f"[clip {clip.id}] direct render_one_clip call failed ({e}), "
                f"falling back to rerender_clip_with_edits"
            )
            new_clip_path = None  # clear so we fall through

    if new_clip_path is None:
        # === PATH A3: fall through (rerender without user_crop) ===
        new_clip_path, new_thumb_path = rerender_clip_with_edits(
            source=Path(source_path),
            clip_row=clip,
            output_dir=output_dir,
            job_id=job.id,
            aspect_ratio=aspect_ratio,
            crop_position=crop_position,
            caption_preset=caption_preset,
            detected_language=detected_language,
        )
        logger.info(f"[clip {clip.id}] rendered via path A3 (rerender without user_crop)")

    # === Update DB ===
    clip.file_path = str(new_clip_path)
    if new_thumb_path:
        clip.thumbnail_path = str(new_thumb_path)
    db.commit()

    # === Warning logic — only warn if user_crop was set BUT none of the paths
    # successfully applied it ===
    user_crop_was_applied = (
        user_crop_from_meta is None  # nothing to apply, vacuously OK
        or rerender_accepts_user_crop  # A1
        or used_direct_render_one_clip  # A2
    )

    if not user_crop_was_applied:
        logger.error(
            f"[clip {clip.id}] ⚠ REFRAME WILL NOT TAKE EFFECT — "
            f"render.py is NOT patched for user_crop support. "
            f"Run: python patch_render_for_reframe.py from the repo root, "
            f"then restart the backend and click Reframe → Save again. "
            f"The clip was re-rendered but with default smart_crop framing, "
            f"NOT with the user_crop the user picked."
        )
        meta = dict(clip.meta or {})
        meta["_reframe_warning"] = (
            "Reframe was saved but render.py is not patched — re-rendered with "
            "default crop instead of your chosen crop. Run "
            "patch_render_for_reframe.py and restart the backend."
        )
        clip.meta = meta
        flag_modified(clip, "meta")
        db.commit()
    else:
        # Clear any prior warning — reframe successfully applied this time
        if (clip.meta or {}).get("_reframe_warning"):
            meta = dict(clip.meta)
            meta.pop("_reframe_warning", None)
            clip.meta = meta
            flag_modified(clip, "meta")
            db.commit()


def _clip_to_out(clip: Clip) -> ClipOut:
    return ClipOut(
        id=clip.id,
        video_job_id=clip.video_job_id,
        rank=clip.rank,
        title=clip.title,
        hook=clip.hook,
        reason=clip.reason,
        start_sec=clip.start_sec,
        end_sec=clip.end_sec,
        duration_sec=clip.duration_sec,
        emotion=clip.emotion,
        virality_score=clip.virality_score,
        meta=dict(clip.meta or {}),
        # Step 13: editable fields
        original_transcript=list(clip.original_transcript or []) if clip.original_transcript else None,
        edited_transcript=list(clip.edited_transcript or []) if clip.edited_transcript else None,
        needs_rerender=bool(clip.needs_rerender),
        rerender_count=int(clip.rerender_count or 0),
        download_url=f"/clips/{clip.id}/download" if clip.file_path else None,
        thumbnail_url=f"/clips/{clip.id}/thumbnail" if clip.thumbnail_path else None,
        created_at=clip.created_at,
    )


@router.get("/job/{job_id}", response_model=ClipListOut, summary="List clips for a job")
def list_clips_for_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClipListOut:
    clips = (
        db.query(Clip)
        .filter(Clip.video_job_id == job_id, Clip.user_id == current_user.id)
        .order_by(Clip.rank)
        .all()
    )
    return ClipListOut(clips=[_clip_to_out(c) for c in clips], total=len(clips))


@router.get("/{clip_id}", response_model=ClipOut, summary="Get single clip details")
def get_clip(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClipOut:
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    return _clip_to_out(clip)


@router.put(
    "/{clip_id}/transcript",
    response_model=ClipOut,
    summary="Update a clip's transcript (user edits). Sets needs_rerender=True.",
)
def update_clip_transcript(
    clip_id: uuid.UUID,
    payload: TranscriptUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClipOut:
    """
    User saves their edited transcript. Does NOT trigger re-render.
    Re-render is a separate call so user can save drafts without burning CPU.
    """
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    # Validate + sanitize: segments may overlap the clip boundary (e.g., a segment
    # that starts 5s before the clip's start_sec if the Whisper segment straddled
    # the boundary). We accept overlaps but CLAMP stored times to clip range,
    # and reject only truly out-of-range segments (more than 10s outside either side).
    OVERLAP_TOLERANCE = 10.0  # seconds — allows Whisper segments that cross clip boundaries

    sanitized = []
    for seg in payload.segments:
        # Reject segments that are way outside the clip window (likely a bug)
        if seg.end < clip.start_sec - OVERLAP_TOLERANCE:
            raise HTTPException(
                400,
                detail=(
                    f"Segment ends at {seg.end:.1f}s, which is more than {OVERLAP_TOLERANCE:.0f}s "
                    f"before the clip starts ({clip.start_sec:.1f}s). Invalid range."
                ),
            )
        if seg.start > clip.end_sec + OVERLAP_TOLERANCE:
            raise HTTPException(
                400,
                detail=(
                    f"Segment starts at {seg.start:.1f}s, which is more than {OVERLAP_TOLERANCE:.0f}s "
                    f"after the clip ends ({clip.end_sec:.1f}s). Invalid range."
                ),
            )
        if not seg.text.strip():
            raise HTTPException(400, detail="Segment text cannot be empty.")
        if len(seg.text) > 1000:
            raise HTTPException(400, detail="Segment text too long (max 1000 chars).")

        # CLAMP segment times to the clip window so downstream render always
        # gets clean, in-range segments. Overlapping Whisper segments are fine
        # for captioning but we want the stored data to be tidy.
        clamped_start = max(clip.start_sec, min(seg.start, clip.end_sec))
        clamped_end = max(clip.start_sec, min(seg.end, clip.end_sec))
        # If clamping collapsed the segment to zero duration, skip it
        if clamped_end - clamped_start < 0.05:
            continue

        sanitized.append({
            "start": clamped_start,
            "end": clamped_end,
            "text": seg.text.strip(),
        })

    if not sanitized:
        raise HTTPException(
            400,
            detail="After validation, no usable segments remain. Check your edits.",
        )

    # Store edits (already sanitized dicts)
    clip.edited_transcript = sanitized
    flag_modified(clip, "edited_transcript")
    clip.needs_rerender = True
    db.commit()
    db.refresh(clip)

    logger.info(
        f"[clip {clip_id}] transcript edited by user {current_user.id}: "
        f"{len(sanitized)} segments, needs_rerender=True"
    )

    return _clip_to_out(clip)


@router.post(
    "/{clip_id}/rerender",
    response_model=RerenderResponse,
    summary="Re-render a clip with edited captions",
)
def rerender_clip(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RerenderResponse:
    """
    Trigger a background re-render of this clip with user's edited transcript.
    Returns 'started' — frontend polls GET /clips/{id} for completion.

    The worker task updates clip.file_path, clip.rerender_count on success,
    and clip.needs_rerender=False.
    """
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    if not clip.edited_transcript:
        raise HTTPException(
            409,
            detail="No edits saved. Save your transcript changes first.",
        )

    # Dispatch to Celery worker (non-blocking)
    from backend.pipeline.worker import rerender_clip_task
    try:
        task = rerender_clip_task.delay(str(clip_id))
        logger.info(f"[clip {clip_id}] rerender task dispatched: {task.id}")
    except Exception as e:
        logger.exception(f"[clip {clip_id}] failed to dispatch rerender task")
        raise HTTPException(500, detail=f"Could not queue re-render: {e}")

    return RerenderResponse(
        clip_id=clip_id,
        status="started",
        rerender_count=clip.rerender_count or 0,
        message="Re-render in progress. This usually takes 30-60 seconds.",
    )


@router.get("/{clip_id}/download", summary="Download rendered clip (with optional quality)")
def download_clip(
    clip_id: uuid.UUID,
    quality: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Download the rendered clip.

    Without `quality` parameter (default): returns the raw rendered clip
    (whatever quality the render pipeline produced — typically large file).

    With `quality=480p|720p|1080p`: returns a re-encoded version at that
    quality. If a fresh cached export exists, serves it directly. Otherwise
    encodes synchronously (15-60s wait — frontend should show progress).

    Use the dedicated `/export/options` + `/export` endpoints for async
    high-quality exports with polling. This endpoint is the simple sync path.
    """
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.file_path:
        raise HTTPException(409, detail="Clip not yet rendered")
    path = Path(clip.file_path)
    if not path.exists():
        raise HTTPException(410, detail="Clip file missing on server")

    # Default behavior: return the raw rendered clip
    if not quality:
        clip.download_count = (clip.download_count or 0) + 1
        db.commit()
        return FileResponse(
            path=str(path), media_type="video/mp4",
            filename=f"clip_{clip.rank:02d}.mp4",
        )

    # Quality-tiered download
    from backend.pipeline.clip_export import (
        QUALITY_PROFILES, output_path_for, export_clip, ExportError,
    )
    if quality not in QUALITY_PROFILES:
        raise HTTPException(
            400,
            detail=f"Invalid quality. Must be one of: {sorted(QUALITY_PROFILES.keys())}",
        )

    # Check cache first
    cached = output_path_for(path, quality)
    is_fresh = (
        cached.exists()
        and cached.stat().st_size > 0
        and cached.stat().st_mtime >= path.stat().st_mtime
    )

    if is_fresh:
        out_path = cached
    else:
        # Synchronous encode (blocks until done — could be 15-60s)
        try:
            out_path = export_clip(path, quality)
        except ExportError as e:
            logger.exception(f"[clip {clip_id}] inline export failed")
            raise HTTPException(500, detail=f"Encoding failed: {e}")

    clip.download_count = (clip.download_count or 0) + 1
    db.commit()
    return FileResponse(
        path=str(out_path), media_type="video/mp4",
        filename=f"clip_{clip.rank:02d}_{quality}.mp4",
    )


@router.get("/{clip_id}/thumbnail", summary="Get clip thumbnail")
def get_thumbnail(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.thumbnail_path:
        raise HTTPException(404, detail="No thumbnail")
    path = Path(clip.thumbnail_path)
    if not path.exists():
        raise HTTPException(410, detail="Thumbnail file missing")
    return FileResponse(path=str(path), media_type="image/jpeg")


# ==============================================================================
# Session B2: Canvas customization
# ==============================================================================
# Allow users to add meme-style top text or a thumbnail image above the clip.
# The wrapped result replaces the original clip file.

# Max thumbnail upload size (MB)
_CANVAS_MAX_IMAGE_MB = 5
_CANVAS_ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_CANVAS_MAX_TEXT_LEN = 300


@router.post(
    "/{clip_id}/canvas",
    summary="Apply canvas wrap (meme-style top text or thumbnail image)",
)
async def apply_canvas(
    clip_id: uuid.UUID,
    top_text: str = Form(""),
    layout: str = Form("default"),
    text_size: str = Form("medium"),       # "small" | "medium" | "large" (legacy enum)
    text_size_mult: float = Form(1.0),     # Session FIX-SLIDER: 0.6-1.6 continuous multiplier (preferred)
    text_bold: bool = Form(True),
    text_underline: bool = Form(False),
    text_color: str = Form("white"),       # "white" | "red" | "yellow" | "green"
    thumbnail: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Apply a canvas wrap to a clip. Either top_text OR thumbnail (or both-text-only)
    can be provided. If both are sent, thumbnail wins. Empty string + no image
    removes any existing canvas (reverts to plain clip).

    Pattern of use: user customizes in UI → hits Save → this endpoint runs
    synchronously (canvas wrap is fast, ~5-15s per clip, no Celery needed).
    """
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.file_path:
        raise HTTPException(409, detail="Clip not yet rendered")

    clip_path = Path(clip.file_path)
    if not clip_path.exists():
        raise HTTPException(410, detail="Clip file missing on server")

    # Validate inputs
    top_text = (top_text or "").strip()
    if len(top_text) > _CANVAS_MAX_TEXT_LEN:
        raise HTTPException(400, detail=f"Text too long (max {_CANVAS_MAX_TEXT_LEN} chars)")

    if layout not in ("default", "wide_top", "image_top"):
        layout = "default"
    if text_size not in ("small", "medium", "large"):
        text_size = "medium"
    if text_color not in ("white", "red", "yellow", "green"):
        text_color = "white"
    # Clamp continuous size multiplier to safe range
    try:
        text_size_mult = float(text_size_mult)
    except (TypeError, ValueError):
        text_size_mult = 1.0
    text_size_mult = max(0.5, min(2.0, text_size_mult))

    # Handle uploaded thumbnail
    thumb_path: Path | None = None
    if thumbnail and thumbnail.filename:
        ext = Path(thumbnail.filename).suffix.lower()
        if ext not in _CANVAS_ALLOWED_IMAGE_EXTS:
            raise HTTPException(
                400,
                detail=f"Thumbnail must be {', '.join(_CANVAS_ALLOWED_IMAGE_EXTS)}",
            )
        # Read (size-limited)
        data = await thumbnail.read(_CANVAS_MAX_IMAGE_MB * 1024 * 1024 + 1)
        if len(data) > _CANVAS_MAX_IMAGE_MB * 1024 * 1024:
            raise HTTPException(
                413,
                detail=f"Thumbnail too large (max {_CANVAS_MAX_IMAGE_MB}MB)",
            )
        thumb_path = clip_path.parent / f"canvas_thumb_{clip.rank:02d}{ext}"
        thumb_path.write_bytes(data)

    # If no text and no image, this is a REVERT request — delete existing canvas
    # by restoring the pre-canvas backup (if any). For simplicity we just return
    # early; the UI can call rerender to produce a fresh clip.
    if not top_text and thumb_path is None:
        raise HTTPException(
            400,
            detail="Provide either top_text or thumbnail. To revert, re-render the clip.",
        )

    # Produce the canvas-wrapped output. We write to a temp file first, then
    # atomically replace the clip file to avoid partial-state if ffmpeg fails mid-run.
    from backend.pipeline.canvas import wrap_clip_with_canvas, CanvasError
    import shutil

    temp_out = clip_path.with_suffix(".canvas_tmp.mp4")
    # Session FIX-COMPOUND: trust-tracked backup logic.
    #
    # Bug history: previous logic created backup on first observed apply, but
    # in production users had clips that were ALREADY canvased before the
    # backup logic was deployed. Their first observed "apply" copied a
    # canvased file as the backup → all future applies compounded text on top.
    #
    # Fix: track backup trust in clip.meta. The backup is "trusted" only if
    # WE created it with full knowledge of the clip state at that time AND
    # the clip was NOT already canvased.
    #
    # Decision tree for canvas_input:
    #   1. Trusted backup exists           → use it (ideal path)
    #   2. Untrusted backup + clip not canvased → use clip_path, mark trusted
    #   3. Untrusted backup + clip IS canvased → CONTAMINATION; treat as case (4)
    #   4. No backup, clip not canvased    → save backup, use clip_path
    #   5. No backup, clip IS canvased     → CONTAMINATION; trigger source rerender
    original_backup = clip_path.with_suffix(".original.mp4")
    meta_now = clip.meta or {}
    canvas_meta = meta_now.get("canvas") or {}
    is_currently_canvased = bool(canvas_meta.get("applied")) or bool(meta_now.get("has_user_outro"))
    is_backup_trusted = bool(meta_now.get("canvas_backup_trusted")) and original_backup.exists()

    if is_backup_trusted:
        # Ideal: backup we created earlier — use it
        canvas_input = original_backup
        logger.info(f"[clip {clip_id}] using trusted backup as canvas input")
    elif is_currently_canvased:
        # Contamination: clip is already canvased and we don't have a clean
        # backup. We cannot safely apply canvas without compounding, so we
        # must rebuild the clip from the source video first.
        logger.warning(
            f"[clip {clip_id}] canvas state contaminated — rebuilding from source"
        )
        try:
            _rebuild_clip_from_source(clip, db)
            # After rebuild, clip_path now contains a fresh raw clip.
            # Save as backup and use it as our canvas input.
            shutil.copy2(str(clip_path), str(original_backup))
            canvas_input = original_backup
            logger.info(f"[clip {clip_id}] rebuilt from source, fresh backup saved")
        except Exception as e:
            logger.exception(f"[clip {clip_id}] source rebuild failed")
            raise HTTPException(
                500,
                detail=(
                    "This clip's customization history is corrupted and we "
                    "could not rebuild it. Please regenerate this video. "
                    f"({type(e).__name__})"
                ),
            )
    else:
        # Clean state: clip is raw (or in a state where saving as backup is safe)
        try:
            shutil.copy2(str(clip_path), str(original_backup))
            logger.info(f"[clip {clip_id}] saved fresh trusted backup")
        except OSError as e:
            logger.warning(f"[clip {clip_id}] could not save backup: {e}")
        canvas_input = original_backup if original_backup.exists() else clip_path

    try:
        stats = wrap_clip_with_canvas(
            input_clip=canvas_input,
            output_clip=temp_out,
            job_id=clip.video_job_id or uuid.uuid4(),
            rank=clip.rank,
            top_text=top_text,
            thumbnail_image=thumb_path,
            layout=layout,
            text_size=text_size,
            text_size_mult=text_size_mult,
            text_bold=text_bold,
            text_underline=text_underline,
            text_color=text_color,
        )
    except CanvasError as e:
        # Clean up partial temp file + thumb
        temp_out.unlink(missing_ok=True)
        if thumb_path:
            thumb_path.unlink(missing_ok=True)
        raise HTTPException(500, detail=f"Canvas rendering failed: {e}")

    if stats.get("skipped"):
        temp_out.unlink(missing_ok=True)
        raise HTTPException(400, detail=stats.get("reason", "Canvas wrap skipped"))

    # Replace original clip atomically
    try:
        shutil.move(str(temp_out), str(clip_path))
    except OSError as e:
        temp_out.unlink(missing_ok=True)
        raise HTTPException(500, detail=f"Could not save canvas clip: {e}")

    # Session QUALITY: invalidate cached quality exports since clip content changed
    try:
        from backend.pipeline.clip_export import cleanup_exports
        cleanup_exports(clip_path)
    except Exception:
        pass

    # Persist canvas settings on clip meta (for UI to show current state)
    meta = dict(clip.meta or {})
    meta["canvas"] = {
        "applied": True,
        "top_text": top_text,
        "has_thumbnail": thumb_path is not None,
        "layout": layout,
        "text_size": text_size,
        "text_size_mult": text_size_mult,
        "text_bold": text_bold,
        "text_underline": text_underline,
        "text_color": text_color,
    }
    # Session FIX-COMPOUND: mark backup as trusted ONLY if it actually exists.
    # Future applies use this flag to decide whether they can safely re-render
    # from the backup without compounding.
    meta["canvas_backup_trusted"] = original_backup.exists()
    clip.meta = meta
    flag_modified(clip, "meta")
    db.commit()

    logger.info(
        f"[clip {clip_id}] canvas applied: "
        f"layout={layout}, text_len={len(top_text)}, "
        f"thumb={'yes' if thumb_path else 'no'}"
    )

    return {
        "clip_id": str(clip_id),
        "status": "applied",
        "layout": layout,
        "had_text": bool(top_text),
        "had_image": thumb_path is not None,
    }


@router.delete(
    "/{clip_id}/canvas",
    summary="Reset a clip back to its pre-canvas original",
)
async def reset_canvas(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Restore the clip from the .original backup we saved on first canvas apply.
    Clears the canvas/outro flags from clip.meta. If no backup exists (no canvas
    was ever applied), this is a no-op that returns success.
    """
    import shutil
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.file_path:
        raise HTTPException(409, detail="Clip not yet rendered")

    clip_path = Path(clip.file_path)
    original_backup = clip_path.with_suffix(".original.mp4")

    meta_now = clip.meta or {}
    canvas_meta = meta_now.get("canvas") or {}
    is_canvased = bool(canvas_meta.get("applied")) or bool(meta_now.get("has_user_outro"))
    is_backup_trusted = bool(meta_now.get("canvas_backup_trusted")) and original_backup.exists()

    if not is_canvased and not original_backup.exists():
        # Truly nothing to reset
        return {
            "clip_id": str(clip_id),
            "status": "no_changes",
            "message": "This clip has no canvas customizations to reset",
        }

    if is_backup_trusted:
        # Fast path: trusted backup exists, just copy it back
        try:
            shutil.copy2(str(original_backup), str(clip_path))
            logger.info(f"[clip {clip_id}] restored from trusted backup")
        except OSError as e:
            raise HTTPException(500, detail=f"Could not restore original: {e}")
    else:
        # Slow but reliable path: rebuild from source video. This handles legacy
        # clips that were canvased before backup logic was deployed (so their
        # `.original.mp4` may itself be canvased / contaminated).
        logger.info(
            f"[clip {clip_id}] no trusted backup — rebuilding from source video"
        )
        # Delete any contaminated backup so apply_canvas re-creates a clean one
        if original_backup.exists():
            try:
                original_backup.unlink()
                logger.info(f"[clip {clip_id}] removed potentially contaminated backup")
            except OSError as e:
                logger.warning(f"[clip {clip_id}] could not remove backup: {e}")
        try:
            _rebuild_clip_from_source(clip, db)
        except Exception as e:
            logger.exception(f"[clip {clip_id}] source rebuild failed during reset")
            raise HTTPException(
                500,
                detail=(
                    "Could not rebuild this clip from its source video. "
                    "Please regenerate the entire video. "
                    f"({type(e).__name__})"
                ),
            )

    # Session QUALITY: stale quality exports must be invalidated since the
    # underlying clip content has changed. cleanup_exports removes the
    # .480p/.720p/.1080p derivatives — next export will re-encode fresh.
    try:
        from backend.pipeline.clip_export import cleanup_exports
        removed = cleanup_exports(clip_path)
        if removed:
            logger.info(f"[clip {clip_id}] removed {removed} stale quality exports")
    except Exception as e:
        logger.warning(f"[clip {clip_id}] could not clean exports: {e}")

    # Clear canvas + outro flags so the UI reflects the reset state.
    # Crucially also clear canvas_backup_trusted so the next apply_canvas treats
    # this as a fresh start.
    meta = dict(clip.meta or {})
    meta.pop("canvas", None)
    meta.pop("has_user_outro", None)
    meta.pop("user_outro_duration", None)
    meta.pop("canvas_backup_trusted", None)
    clip.meta = meta
    flag_modified(clip, "meta")
    db.commit()

    logger.info(f"[clip {clip_id}] reset to pre-canvas original (clean)")
    return {
        "clip_id": str(clip_id),
        "status": "reset",
        "message": "Clip restored to original — all customizations cleared",
    }


# =============================================================================
# Session FINAL: Custom outro upload — append user-provided ending video
# =============================================================================
_OUTRO_ALLOWED_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
_OUTRO_MAX_SIZE_MB = 30


@router.post(
    "/{clip_id}/outro",
    summary="Append a user-provided outro video to the end of a clip",
)
async def append_outro(
    clip_id: uuid.UUID,
    outro: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Append a user-uploaded outro/branding video to the end of a clip.

    The outro is concatenated with no fade — just appended cleanly. Useful for
    creators who want their personal branding clip / channel logo at the end of
    every video, giving them a sense of ownership without the system imposing
    one.

    Implementation: re-encode-on-concat (ensures consistent codec params),
    atomic temp+rename so a failed run leaves the original intact.
    """
    import shutil
    import subprocess

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.file_path:
        raise HTTPException(409, detail="Clip not yet rendered")

    clip_path = Path(clip.file_path)
    if not clip_path.exists():
        raise HTTPException(410, detail="Clip file missing on server")

    # Validate outro upload
    if not outro.filename:
        raise HTTPException(400, detail="Outro filename required")
    ext = Path(outro.filename).suffix.lower()
    if ext not in _OUTRO_ALLOWED_EXTS:
        raise HTTPException(
            400,
            detail=f"Outro must be {', '.join(sorted(_OUTRO_ALLOWED_EXTS))}",
        )

    # Read with size limit
    max_bytes = _OUTRO_MAX_SIZE_MB * 1024 * 1024
    data = await outro.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, detail=f"Outro too large (max {_OUTRO_MAX_SIZE_MB} MB)")
    if not data:
        raise HTTPException(400, detail="Outro file is empty")

    outro_path = clip_path.parent / f"outro_{clip.rank:02d}{ext}"
    outro_path.write_bytes(data)

    # Verify outro is a valid playable video
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(outro_path)],
        capture_output=True, text=True, timeout=15,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        outro_path.unlink(missing_ok=True)
        raise HTTPException(400, detail="Outro file is not a valid video")

    try:
        outro_duration = float(probe.stdout.strip())
    except ValueError:
        outro_path.unlink(missing_ok=True)
        raise HTTPException(400, detail="Could not read outro duration")

    if outro_duration > 30:
        outro_path.unlink(missing_ok=True)
        raise HTTPException(400, detail="Outro must be 30 seconds or shorter")

    # Concat using filter_complex (re-encodes both — guarantees consistent params).
    # We scale the outro to match the clip's resolution so concat doesn't fail on
    # mismatched dimensions. SAR is forced to 1 to avoid stretched audio.
    temp_out = clip_path.with_suffix(".outro_tmp.mp4")

    # Probe the clip dimensions so we can scale outro to match
    clip_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(clip_path)],
        capture_output=True, text=True, timeout=15,
    )
    if clip_probe.returncode != 0:
        outro_path.unlink(missing_ok=True)
        raise HTTPException(500, detail="Could not probe clip dimensions")
    try:
        cw, ch = clip_probe.stdout.strip().split("x")
        cw, ch = int(cw), int(ch)
    except ValueError:
        outro_path.unlink(missing_ok=True)
        raise HTTPException(500, detail="Could not parse clip dimensions")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-i", str(outro_path),
        "-filter_complex",
        # Scale outro to match clip dims (CONTAIN — letterbox if aspect differs).
        # Then concat both video+audio streams. Using `aevalsrc=0` ensures audio
        # exists even on silent outros so the audio stream count matches.
        f"[1:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
        f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[outv];"
        f"[0:v]setsar=1,fps=30[clipv];"
        f"[clipv][0:a][outv][1:a]concat=n=2:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(temp_out),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        temp_out.unlink(missing_ok=True)
        outro_path.unlink(missing_ok=True)
        raise HTTPException(500, detail="Outro append timed out")

    if result.returncode != 0:
        # If the outro has no audio track, the filter graph above breaks.
        # Retry with a silent audio fallback for the outro.
        cmd_no_audio = [
            "ffmpeg", "-y",
            "-i", str(clip_path),
            "-i", str(outro_path),
            "-f", "lavfi", "-t", str(outro_duration),
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            f"[1:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[outv];"
            f"[0:v]setsar=1,fps=30[clipv];"
            f"[clipv][0:a][outv][2:a]concat=n=2:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-loglevel", "error",
            str(temp_out),
        ]
        result2 = subprocess.run(cmd_no_audio, capture_output=True, text=True, timeout=300)
        if result2.returncode != 0:
            temp_out.unlink(missing_ok=True)
            outro_path.unlink(missing_ok=True)
            err_tail = (result.stderr or "")[-300:]
            raise HTTPException(500, detail=f"Outro append failed: {err_tail}")

    # Clean up the uploaded outro source (the concat is the only thing we keep)
    outro_path.unlink(missing_ok=True)

    # Atomically replace the clip file
    try:
        shutil.move(str(temp_out), str(clip_path))
    except OSError as e:
        temp_out.unlink(missing_ok=True)
        raise HTTPException(500, detail=f"Could not save concatenated clip: {e}")

    # Session QUALITY: invalidate cached exports since clip content changed
    try:
        from backend.pipeline.clip_export import cleanup_exports
        cleanup_exports(clip_path)
    except Exception:
        pass

    # Mark the clip as having a custom outro
    if clip.meta is None:
        clip.meta = {}
    clip.meta["has_user_outro"] = True
    clip.meta["user_outro_duration"] = round(outro_duration, 2)
    flag_modified(clip, "meta")
    db.commit()

    logger.info(
        f"[clip {clip_id}] user outro appended: "
        f"+{outro_duration:.1f}s, scaled to {cw}x{ch}"
    )
    return {
        "clip_id": str(clip_id),
        "status": "appended",
        "outro_duration": round(outro_duration, 2),
    }


# =============================================================================
# Session QUALITY: tiered video export (480p / 720p / 1080p)
# =============================================================================
@router.get(
    "/{clip_id}/export/options",
    summary="List quality export options with size estimates",
)
def list_export_options(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns the 3 quality tiers with estimated output sizes for THIS clip.
    Estimates are based on clip duration × target bitrate. Real sizes will
    vary by content complexity (motion, detail).
    """
    from backend.pipeline.clip_export import (
        QUALITY_PROFILES, estimate_export_size_bytes, output_path_for, get_clip_duration,
    )

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.file_path:
        raise HTTPException(409, detail="Clip not yet rendered")
    path = Path(clip.file_path)
    if not path.exists():
        raise HTTPException(410, detail="Clip file missing on server")

    duration = get_clip_duration(path)

    options = []
    for q, profile in QUALITY_PROFILES.items():
        est_bytes = estimate_export_size_bytes(path, q)
        # Check cache: if a fresh export exists for this quality, mark it
        cached_path = output_path_for(path, q)
        is_cached = (
            cached_path.exists()
            and cached_path.stat().st_size > 0
            and cached_path.stat().st_mtime >= path.stat().st_mtime
        )
        actual_size_mb = (
            round(cached_path.stat().st_size / (1024 * 1024), 2)
            if is_cached else None
        )
        options.append({
            "quality": q,
            "label": profile["label"],
            "est_size_bytes": est_bytes,
            "est_size_mb": round(est_bytes / (1024 * 1024), 2) if est_bytes else None,
            "cached": is_cached,
            "actual_size_mb": actual_size_mb,
        })
    return {
        "clip_id": str(clip_id),
        "duration_sec": round(duration, 2),
        "options": options,
    }


@router.post(
    "/{clip_id}/export",
    summary="Start an async export job at chosen quality",
)
def start_export(
    clip_id: uuid.UUID,
    quality: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Kick off a Celery export task. Returns an export_id the client polls.
    If a fresh cached export exists, returns status='complete' immediately
    so the client can skip the polling loop.
    """
    from backend.pipeline.clip_export import (
        QUALITY_PROFILES, output_path_for,
    )
    from backend.pipeline.worker import (
        export_clip_quality, set_export_status, _redis_client,
    )
    import secrets

    if quality not in QUALITY_PROFILES:
        raise HTTPException(
            400,
            detail=f"Invalid quality. Must be one of: {sorted(QUALITY_PROFILES.keys())}",
        )

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    if not clip.file_path:
        raise HTTPException(409, detail="Clip not yet rendered")
    path = Path(clip.file_path)
    if not path.exists():
        raise HTTPException(410, detail="Clip file missing on server")

    # Cache short-circuit: if encoded file already exists and is fresh, return immediately
    cached_path = output_path_for(path, quality)
    if (cached_path.exists()
        and cached_path.stat().st_size > 0
        and cached_path.stat().st_mtime >= path.stat().st_mtime):
        export_id = secrets.token_urlsafe(12)
        set_export_status(
            export_id,
            status="complete",
            progress=100,
            clip_id=str(clip_id),
            quality=quality,
            output_path=str(cached_path),
            output_size_mb=round(cached_path.stat().st_size / (1024 * 1024), 2),
            cached=True,
        )
        return {
            "export_id": export_id,
            "status": "complete",
            "cached": True,
        }

    # Queue an async export
    export_id = secrets.token_urlsafe(12)
    set_export_status(
        export_id,
        status="queued",
        progress=0,
        clip_id=str(clip_id),
        quality=quality,
    )
    export_clip_quality.delay(export_id, str(clip_id), quality)

    return {
        "export_id": export_id,
        "status": "queued",
        "cached": False,
    }


@router.get(
    "/{clip_id}/export/{export_id}",
    summary="Poll export status",
)
def poll_export(
    clip_id: uuid.UUID,
    export_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns the current status of an export job. Frontend polls this every
    2-3 seconds while status is 'queued' or 'encoding'.
    """
    from backend.pipeline.worker import get_export_status

    # Verify clip ownership (so user can't read someone else's export status)
    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    status = get_export_status(export_id)
    if status is None:
        raise HTTPException(404, detail="Export not found or expired")

    # Sanity: confirm this export belongs to this clip (defense in depth)
    if status.get("clip_id") and status["clip_id"] != str(clip_id):
        raise HTTPException(403, detail="Export does not belong to this clip")

    return status


@router.get(
    "/{clip_id}/export/{export_id}/download",
    summary="Download the encoded export",
)
def download_export(
    clip_id: uuid.UUID,
    export_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Stream the encoded file when status is 'complete'. Returns 409 if not yet
    ready (frontend should keep polling) or 404 if export doesn't exist.
    """
    from backend.pipeline.worker import get_export_status

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    status = get_export_status(export_id)
    if status is None:
        raise HTTPException(404, detail="Export not found or expired")
    if status.get("status") != "complete":
        raise HTTPException(
            409,
            detail=f"Export not ready (status: {status.get('status', 'unknown')})",
        )

    out_path = Path(status.get("output_path", ""))
    if not out_path.exists():
        raise HTTPException(410, detail="Encoded file missing on server")

    # Bump download count on the underlying clip
    clip.download_count = (clip.download_count or 0) + 1
    db.commit()

    quality = status.get("quality", "export")
    filename = f"clip_{clip.rank:02d}_{quality}.mp4"
    return FileResponse(
        path=str(out_path),
        media_type="video/mp4",
        filename=filename,
    )



# =============================================================================
# Session REFRAME: User-controlled crop adjustment
# =============================================================================
# Three endpoints work together:
#   GET  /clips/{id}/source-info       Returns source dims + clip duration
#   GET  /clips/{id}/source-frame      Extracts a frame from source at t_pct
#   POST /clips/{id}/reframe           Saves user_crop + rebuilds clip from source
#
# user_crop is stored on clip.meta as:
#     {"x_pct": 0.45, "y_pct": 0.5, "zoom": 1.0}
# The render pipeline (after running patch_render_for_reframe.py) reads this
# from clip.meta and applies it via build_smart_crop_filter.

from pydantic import BaseModel, Field
from typing import List, Literal, Optional


class ReframeKeyframe(BaseModel):
    """A single keyframe in a per-segment reframe."""
    t: float = Field(..., ge=0.0, description="Time in seconds (clip-relative)")
    x_pct: float = Field(..., ge=0.0, le=1.0, description="Crop center X (0..1)")
    y_pct: float = Field(0.5, ge=0.0, le=1.0, description="Crop center Y (0..1)")


class ReframeRequest(BaseModel):
    """
    Reframe request — supports both single-crop (v1) and keyframe (v2) formats.

    v1 (legacy / simple): set x_pct + y_pct + zoom. Single static crop.
    v2 (keyframes): set keyframes=[...] + transition + zoom. Time-varying crop
        for multi-speaker clips.

    If both are provided, keyframes wins.
    """
    # v1: single static crop
    x_pct: Optional[float] = Field(None, ge=0.0, le=1.0)
    y_pct: Optional[float] = Field(None, ge=0.0, le=1.0)
    # v2: keyframe timeline
    keyframes: Optional[List[ReframeKeyframe]] = None
    transition: Literal["smooth", "cut"] = "smooth"
    transition_dur: float = Field(0.4, ge=0.0, le=2.0)
    # Shared
    zoom: float = Field(1.0, ge=1.0, le=4.0)


@router.get(
    "/{clip_id}/source-info",
    summary="Get source video dimensions and clip duration (for reframe modal)",
)
def get_source_info(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns the source video's pixel dimensions plus the clip's duration.
    Used by the Reframe modal to size the drag overlay correctly.
    """
    from backend.models.video import VideoJob
    from backend.pipeline.reframe import get_video_dimensions

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None or not job.file_path:
        raise HTTPException(409, detail="Source video not found for this clip")

    source_path = Path(job.file_path)
    if not source_path.exists():
        raise HTTPException(410, detail="Source video missing on disk")

    sw, sh = get_video_dimensions(source_path)
    if sw == 0 or sh == 0:
        raise HTTPException(500, detail="Could not probe source video dimensions")

    duration = float(clip.duration_sec or (clip.end_sec - clip.start_sec) or 0)

    # Existing user_crop (if any) so the modal opens with current values
    meta = clip.meta or {}
    user_crop = meta.get("user_crop")

    return {
        "clip_id": str(clip_id),
        "source_width": sw,
        "source_height": sh,
        "duration_sec": duration,
        "start_sec": float(clip.start_sec or 0),
        "end_sec": float(clip.end_sec or 0),
        "user_crop": user_crop,
    }


@router.get(
    "/{clip_id}/source-frame",
    summary="Extract a single frame from source video (for reframe preview)",
)
def get_source_frame(
    clip_id: uuid.UUID,
    t_pct: float = 0.5,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Extract one frame at t_pct (0..1) through the clip's time range.
    Returns the JPEG. Cached on disk per (clip_id, t_pct rounded to 3 decimals)
    so scrubbing is fast on repeat fetches.
    """
    from backend.models.video import VideoJob
    from backend.pipeline.reframe import extract_source_frame

    if t_pct < 0:
        t_pct = 0.0
    if t_pct > 1:
        t_pct = 1.0

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None or not job.file_path:
        raise HTTPException(409, detail="Source video not found")

    source_path = Path(job.file_path)
    if not source_path.exists():
        raise HTTPException(410, detail="Source video missing")

    # Compute absolute time in source video
    start = float(clip.start_sec or 0)
    end = float(clip.end_sec or (start + 30))
    abs_time_sec = start + t_pct * (end - start)

    # Cache file: same dir as the rendered clip, with deterministic name
    clip_path = Path(clip.file_path) if clip.file_path else None
    if clip_path:
        cache_dir = clip_path.parent / ".reframe_cache"
    else:
        # Fallback to /tmp if no clip path (shouldn't happen for rendered clips)
        cache_dir = Path("/tmp/reframe_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Round t_pct to 3 decimals so similar scrub positions hit cache
    t_key = f"{t_pct:.3f}"
    cache_file = cache_dir / f"clip_{clip_id}_{t_key}.jpg"

    if not cache_file.exists():
        ok = extract_source_frame(source_path, abs_time_sec, cache_file)
        if not ok:
            raise HTTPException(500, detail="Could not extract source frame")

    # Return the JPEG with appropriate caching headers (per-(clip,t) is immutable)
    return FileResponse(
        path=str(cache_file),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post(
    "/{clip_id}/reframe",
    summary="Save user-chosen crop coordinates and rebuild clip from source",
)
async def reframe_clip(
    clip_id: uuid.UUID,
    payload: ReframeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Save the user's reframe choice on clip.meta.user_crop and rebuild the clip
    from source video. Same pattern as canvas reset:
      - Synchronous (blocks request 30-60s — user explicitly chose this)
      - Clears canvas/outro flags (reframe is destructive — user re-applies after)
      - Removes any cached quality exports

    On rebuild, the smart_crop module reads user_crop and applies precise
    user-specified coordinates instead of running face detection.
    """
    from backend.pipeline.reframe import (
        validate_user_crop, validate_keyframes,
        get_video_dimensions, aspect_ratio_to_float,
    )

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    # Probe source dims for validation/clamping
    from backend.models.video import VideoJob
    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None or not job.file_path:
        raise HTTPException(409, detail="Source video not found")
    source_path = Path(job.file_path)
    if not source_path.exists():
        raise HTTPException(410, detail="Source video missing on disk")

    sw, sh = get_video_dimensions(source_path)
    if sw == 0 or sh == 0:
        raise HTTPException(500, detail="Could not probe source video")

    job_meta = job.meta or {}
    aspect_ratio_str = job_meta.get("aspect_ratio", "9:16")
    target_aspect = aspect_ratio_to_float(aspect_ratio_str)

    clip_duration = float(clip.duration_sec or
                          (clip.end_sec - clip.start_sec) or 60)

    # Build the user_crop dict to persist. v2 (keyframes) is the modern path;
    # v1 (single x_pct/y_pct) is kept for older frontends or simple cases.
    if payload.keyframes and len(payload.keyframes) > 0:
        # v2: keyframes
        kf_dicts = [
            {"t": kf.t, "x_pct": kf.x_pct, "y_pct": kf.y_pct}
            for kf in payload.keyframes
        ]
        validated_kfs = validate_keyframes(
            kf_dicts, sw, sh, target_aspect, payload.zoom, clip_duration
        )
        if not validated_kfs:
            raise HTTPException(400, detail="No valid keyframes provided")
        user_crop = {
            "version": 2,
            "keyframes": validated_kfs,
            "zoom": float(payload.zoom),
            "transition": payload.transition,
            "transition_dur": float(payload.transition_dur),
        }
        log_summary = (
            f"v2 keyframes={len(validated_kfs)} "
            f"transition={payload.transition} zoom={payload.zoom:.2f}"
        )
    elif payload.x_pct is not None and payload.y_pct is not None:
        # v1: single static crop
        x, y, z = validate_user_crop(
            payload.x_pct, payload.y_pct, sw, sh, target_aspect, payload.zoom
        )
        user_crop = {
            "version": 2,
            "keyframes": [{"t": 0.0, "x_pct": x, "y_pct": y}],
            "zoom": z,
            "transition": "cut",
            "transition_dur": 0.0,
        }
        log_summary = f"v1 single x_pct={x:.3f} y_pct={y:.3f} zoom={z:.2f}"
    else:
        raise HTTPException(
            400, detail="Provide either keyframes=[...] or x_pct/y_pct."
        )

    # Persist to meta. Reframe is destructive: clear canvas/outro state.
    meta = dict(clip.meta or {})
    meta["user_crop"] = user_crop
    meta.pop("canvas", None)
    meta.pop("has_user_outro", None)
    meta.pop("user_outro_duration", None)
    meta.pop("canvas_backup_trusted", None)
    clip.meta = meta
    flag_modified(clip, "meta")
    db.commit()

    # Remove old "original" backup + cached exports (stale after reframe)
    if clip.file_path:
        old_backup = Path(clip.file_path).with_suffix(".original.mp4")
        if old_backup.exists():
            try:
                old_backup.unlink()
            except OSError:
                pass
        try:
            from backend.pipeline.clip_export import cleanup_exports
            cleanup_exports(Path(clip.file_path))
        except Exception:
            pass

    logger.info(f"[clip {clip_id}] reframe: {log_summary}")
    try:
        _rebuild_clip_from_source(clip, db)
    except Exception as e:
        logger.exception(f"[clip {clip_id}] reframe rebuild failed")
        raise HTTPException(
            500,
            detail=(
                f"Reframe rebuild failed ({type(e).__name__}). "
                "The crop was saved but the clip could not be re-rendered. "
                "Try the Reset button on the clip card."
            ),
        )

    # Clean stale frame cache for this clip
    if clip.file_path:
        cache_dir = Path(clip.file_path).parent / ".reframe_cache"
        if cache_dir.exists():
            for f in cache_dir.glob(f"clip_{clip_id}_*.jpg"):
                try:
                    f.unlink()
                except OSError:
                    pass

    # Re-fetch clip to pick up any warning the rebuild added
    db.refresh(clip)
    response = {
        "clip_id": str(clip_id),
        "status": "reframed",
        "user_crop": (clip.meta or {}).get("user_crop"),
        "message": "Clip reframed and re-rendered",
    }
    warning = (clip.meta or {}).get("_reframe_warning")
    if warning:
        response["status"] = "reframed_with_warning"
        response["warning"] = warning
        response["message"] = (
            "Clip was re-rendered, but reframe was NOT applied. "
            "See server logs for instructions to fix."
        )

    return response



@router.get(
    "/{clip_id}/detect-speakers",
    summary="Detect speakers in clip and return suggested keyframe positions",
)
def detect_speakers_endpoint(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Auto-detect speakers in the source video for this clip's time range.
    Returns a list of speakers with their normalized X/Y positions.

    The reframe modal calls this on open to suggest auto-keyframe positions.
    User can accept the suggestions, modify them, or start fresh.
    """
    from backend.models.video import VideoJob
    from backend.pipeline.smart_crop import (
        detect_speakers_for_keyframes, get_video_dimensions,
    )

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None or not job.file_path:
        raise HTTPException(409, detail="Source video not found")
    source_path = Path(job.file_path)
    if not source_path.exists():
        raise HTTPException(410, detail="Source video missing on disk")

    sw, sh = get_video_dimensions(source_path)
    if sw == 0 or sh == 0:
        raise HTTPException(500, detail="Could not probe source")

    start = float(clip.start_sec or 0)
    duration = float(clip.duration_sec or (clip.end_sec - clip.start_sec) or 30)

    try:
        speakers = detect_speakers_for_keyframes(
            source_video=source_path,
            clip_start=start, clip_duration=duration,
            source_width=sw, source_height=sh,
            max_speakers=4,
        )
    except Exception as e:
        logger.exception(f"[clip {clip_id}] speaker detection failed")
        return {
            "clip_id": str(clip_id),
            "speakers": [],
            "detection_error": str(e)[:200],
        }

    return {
        "clip_id": str(clip_id),
        "speakers": speakers,
    }



@router.get(
    "/{clip_id}/source-clip-preview",
    summary="Stream a low-res preview of the source clip range (for reframe live preview)",
)
def get_source_clip_preview(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns a low-res, audio-stripped MP4 of the source video for this clip's
    time range. Cached on disk. Used by the reframe modal to play a real video
    preview while the user adjusts keyframes — they see the actual content
    moving as they tweak crop positions.

    Output: 540p max width, no audio (frontend will sync to source video player
    if it wants), MP4 with faststart for streaming.
    """
    import subprocess as _sp
    from backend.models.video import VideoJob

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None or not job.file_path:
        raise HTTPException(409, detail="Source video not found")
    source_path = Path(job.file_path)
    if not source_path.exists():
        raise HTTPException(410, detail="Source video missing on disk")

    start = float(clip.start_sec or 0)
    duration = float(clip.duration_sec or (clip.end_sec - clip.start_sec) or 30)

    # Cache: same dir as the rendered clip
    if clip.file_path:
        cache_dir = Path(clip.file_path).parent / ".reframe_cache"
    else:
        cache_dir = Path("/tmp/reframe_cache") / str(clip.user_id or "anon")
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_file = cache_dir / f"clip_{clip_id}_preview.mp4"

    # Cache check: rebuild if stale
    needs_rebuild = (
        not cache_file.exists()
        or cache_file.stat().st_size == 0
    )
    if not needs_rebuild and source_path.exists():
        # If source is newer than cache, rebuild
        if source_path.stat().st_mtime > cache_file.stat().st_mtime:
            needs_rebuild = True

    if needs_rebuild:
        tmp_file = cache_file.with_suffix(".tmp.mp4")
        # 540p max — small enough to download fast, big enough to look right
        # Audio stripped — saves bandwidth, frontend doesn't need it for preview
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-i", str(source_path),
            "-t", f"{duration:.3f}",
            "-vf", "scale='min(960,iw)':-2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-pix_fmt", "yuv420p",
            "-an",  # no audio
            "-movflags", "+faststart",
            str(tmp_file),
        ]
        result = _sp.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            tmp_file.unlink(missing_ok=True)
            logger.warning(
                f"[clip {clip_id}] preview build failed: "
                f"{result.stderr.decode()[:200]}"
            )
            raise HTTPException(500, detail="Could not build clip preview")
        try:
            import shutil
            shutil.move(str(tmp_file), str(cache_file))
        except OSError as e:
            tmp_file.unlink(missing_ok=True)
            raise HTTPException(500, detail=f"Could not save preview: {e}")

    if not cache_file.exists() or cache_file.stat().st_size == 0:
        raise HTTPException(500, detail="Preview file missing after build")

    return FileResponse(
        path=str(cache_file),
        media_type="video/mp4",
        headers={
            "Cache-Control": "public, max-age=600",
            "Accept-Ranges": "bytes",
        },
    )



@router.get(
    "/{clip_id}/auto-keyframes",
    summary="One-click auto-keyframes: active-speaker pipeline with debounced speaker-change events",
)
def auto_keyframes_endpoint(
    clip_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns keyframes derived from the Phase 2B active-speaker pipeline:
      audio VAD → face tracking → per-second speaker → debounced change events → keyframes.

    Keyframes are clip-relative (t=0 is clip start) and ready for direct use via
    the existing POST /reframe endpoint. Frontend can apply with one click; user
    can fine-tune afterwards.

    Diagnostics field is always present and surfaces pipeline telemetry for debugging.
    """
    from backend.models.video import VideoJob
    from backend.pipeline.smart_crop import get_video_dimensions
    from backend.pipeline.audio_activity import analyze_audio_activity
    from backend.pipeline.active_speaker import (
        track_faces_across_frames,
        compute_active_speaker_timeline,
    )
    from backend.pipeline.conversation_pace import place_adaptive_keyframes

    clip = (
        db.query(Clip)
        .filter(Clip.id == clip_id, Clip.user_id == current_user.id)
        .first()
    )
    if clip is None:
        raise HTTPException(404, detail="Clip not found")

    job = db.query(VideoJob).filter(VideoJob.id == clip.video_job_id).first()
    if job is None or not job.file_path:
        raise HTTPException(409, detail="Source video not found")
    source_path = Path(job.file_path)
    if not source_path.exists():
        raise HTTPException(410, detail="Source video missing on disk")

    sw, sh = get_video_dimensions(source_path)
    if sw == 0 or sh == 0:
        raise HTTPException(500, detail="Could not probe source")

    start    = float(clip.start_sec or 0)
    duration = float(clip.duration_sec or (clip.end_sec - clip.start_sec) or 30)
    clip_end = start + duration

    try:
        audio_activity   = analyze_audio_activity(source_path, job_id=str(clip_id))
        face_tracking    = track_faces_across_frames(source_path)
        speaker_timeline = compute_active_speaker_timeline(face_tracking, audio_activity)
        keyframes        = place_adaptive_keyframes(
            speaker_timeline,
            face_tracking,
            clip_start_sec=start,
            clip_end_sec=clip_end,
            source_width=sw,
            source_height=sh,
        )
    except Exception as e:
        logger.exception(f"[clip {clip_id}] auto-keyframes pipeline failed")
        return {
            "clip_id":        str(clip_id),
            "keyframes":      [],
            "detection_error": str(e)[:200],
            "diagnostics":    {},
        }

    is_voice = audio_activity.get("is_voice", [])
    voice_pct = round(100 * sum(is_voice) / max(1, len(is_voice)), 1)

    return {
        "clip_id":  str(clip_id),
        "keyframes": keyframes,
        "diagnostics": {
            "source_duration_sec":  round(audio_activity.get("duration_sec", 0), 1),
            "voice_pct":            voice_pct,
            "timeline_seconds":     len(speaker_timeline),
            "keyframes_placed":     len(keyframes),
            "source_wh":            [sw, sh],
            "clip_range_sec":       [round(start, 3), round(clip_end, 3)],
        },
    }