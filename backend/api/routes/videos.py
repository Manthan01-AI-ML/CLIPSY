"""
backend/api/routes/videos.py

Video intake — Step 14: platform targeting added.

NOTE: We do NOT use `from __future__ import annotations` here because slowapi +
FastAPI need runtime access to Pydantic model types at decorator evaluation time.
"""
import uuid
from typing import Literal

from fastapi import (
    APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, status, Query
)
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user
from backend.core.config import settings
from backend.core.database import get_db
from backend.core.url_security import validate_youtube_url, UnsafeURLError
from backend.models.user import User
from backend.models.video import VideoJob, SourceType, JobStatus, Goal
from backend.schemas.video import (
    YoutubeSubmitRequest, VideoJobOut, VideoJobListOut, JobCreatedResponse,
)
from backend.services.storage import (
    upload_path, ALLOWED_VIDEO_EXTENSIONS, validate_video_bytes,
)


router = APIRouter()


def _user_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if len(token) > 20:
            return f"user:{token[-20:]}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_user_key)


_GoalStr = Literal["viral", "authority", "lead_gen", "educational"]
_AspectStr = Literal["9:16", "1:1", "16:9"]
_CropStr = Literal["center", "top", "bottom", "left", "right"]
_CaptionStr = Literal["hormozi", "bold", "minimal", "tiktok", "viral", "clean", "dynamic", "hinglish"]
_PlatformStr = Literal["youtube_shorts", "tiktok", "instagram_reels", "all"]  # NEW Step 14


def _check_credits(user: User) -> None:
    if user.credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"No credits left on the '{user.plan.value}' plan. "
                "Upgrade or wait for monthly reset."
            ),
        )


def _enqueue_pipeline(job_id: uuid.UUID) -> None:
    from backend.pipeline.worker import process_video
    process_video.delay(str(job_id))


@router.post(
    "/youtube",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a YouTube URL for clipping",
)
@limiter.limit(settings.RATE_LIMIT_VIDEO_SUBMIT)
def submit_youtube(
    request: Request,
    payload: YoutubeSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobCreatedResponse:
    _check_credits(current_user)

    try:
        clean_url = validate_youtube_url(str(payload.url))
    except UnsafeURLError as e:
        raise HTTPException(status_code=400, detail=f"URL rejected: {e}")

    job = VideoJob(
        user_id=current_user.id,
        source_type=SourceType.youtube,
        source_url=clean_url,
        status=JobStatus.queued,
        goal=Goal(payload.goal),
        meta={
            "aspect_ratio": payload.aspect_ratio,
            "crop_position": payload.crop_position,
            "caption_preset": payload.caption_preset,
            "platform": payload.platform,  # Step 14
            # Session C:
            "add_hook_outro": payload.add_hook_outro,
            "remove_silences": payload.remove_silences,
        },
    )
    db.add(job)
    current_user.credits -= 1
    db.commit()
    db.refresh(job)

    _enqueue_pipeline(job.id)
    return JobCreatedResponse(job_id=job.id, status=job.status.value)


@router.post(
    "/upload",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a video file for clipping",
)
@limiter.limit(settings.RATE_LIMIT_VIDEO_SUBMIT)
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    goal: _GoalStr = Form("viral"),
    aspect_ratio: _AspectStr = Form("9:16"),
    crop_position: _CropStr = Form("center"),
    caption_preset: _CaptionStr = Form("hormozi"),
    platform: _PlatformStr = Form("all"),  # NEW Step 14
    # Session C:
    add_hook_outro: bool = Form(True),
    remove_silences: bool = Form(True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JobCreatedResponse:
    _check_credits(current_user)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename required")

    from pathlib import Path
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed. Accepted: {sorted(ALLOWED_VIDEO_EXTENSIONS)}",
        )

    job = VideoJob(
        user_id=current_user.id,
        source_type=SourceType.upload,
        status=JobStatus.queued,
        goal=Goal(goal),
        meta={
            "original_filename": file.filename,
            "aspect_ratio": aspect_ratio,
            "crop_position": crop_position,
            "caption_preset": caption_preset,
            "platform": platform,  # NEW Step 14
            # Session C:
            "add_hook_outro": add_hook_outro,
            "remove_silences": remove_silences,
        },
    )
    db.add(job)
    db.flush()

    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    dest = upload_path(current_user.id, job.id, file.filename)
    total = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (>{settings.MAX_UPLOAD_SIZE_MB} MB)",
                    )
                out.write(chunk)
    except HTTPException:
        db.rollback()
        raise
    except OSError as e:
        db.rollback()
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"Could not save file: {e}")

    try:
        validate_video_bytes(dest)
    except ValueError as e:
        db.rollback()
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"File rejected: {e}",
        )

    job.file_path = str(dest)
    current_user.credits -= 1
    db.commit()
    db.refresh(job)

    _enqueue_pipeline(job.id)
    return JobCreatedResponse(job_id=job.id, status=job.status.value)


@router.get("/", response_model=VideoJobListOut, summary="List my video jobs")
def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VideoJobListOut:
    q = db.query(VideoJob).filter(VideoJob.user_id == current_user.id)
    total = q.count()
    jobs = q.order_by(VideoJob.created_at.desc()).offset(offset).limit(limit).all()
    return VideoJobListOut(
        jobs=[VideoJobOut.model_validate(j) for j in jobs],
        total=total,
    )


@router.get("/{job_id}", response_model=VideoJobOut, summary="Get job status")
def get_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VideoJobOut:
    job = (
        db.query(VideoJob)
        .filter(VideoJob.id == job_id, VideoJob.user_id == current_user.id)
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return VideoJobOut.model_validate(job)