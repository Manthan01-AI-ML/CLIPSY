"""backend/schemas/clip.py — Step 13: editable transcript support."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TranscriptSegment(BaseModel):
    """A single editable segment. Frontend sends these back on edit."""
    start: float
    end: float
    text: str


class TranscriptUpdateRequest(BaseModel):
    """POST /clips/{id}/transcript body."""
    segments: list[TranscriptSegment] = Field(min_length=1, max_length=200)


class ClipOut(BaseModel):
    id: uuid.UUID
    video_job_id: uuid.UUID
    rank: int
    title: str | None
    hook: str | None
    reason: str | None
    start_sec: float
    end_sec: float
    duration_sec: float
    emotion: str | None
    virality_score: int | None
    meta: dict[str, Any] = Field(default_factory=dict)

    # Step 13: editable caption fields exposed to frontend
    original_transcript: list[dict[str, Any]] | None = None
    edited_transcript: list[dict[str, Any]] | None = None
    needs_rerender: bool = False
    rerender_count: int = 0

    download_url: str | None
    thumbnail_url: str | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ClipListOut(BaseModel):
    clips: list[ClipOut]
    total: int


class RerenderResponse(BaseModel):
    """POST /clips/{id}/rerender response."""
    clip_id: uuid.UUID
    status: str                  # "started", "already_rendering", "failed"
    rerender_count: int
    message: str