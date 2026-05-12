"""
backend/schemas/video.py

Pydantic models — Step 14 adds platform targeting for Sonnet two-pass scoring.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, ConfigDict


AspectRatio = Literal["9:16", "1:1", "16:9"]
CropPosition = Literal["center", "top", "bottom", "left", "right"]
Goal = Literal["viral", "authority", "lead_gen", "educational"]
CaptionPreset = Literal["hormozi", "bold", "minimal", "tiktok", "viral", "clean", "dynamic", "hinglish"]

# NEW Step 14: target platform for clip selection tuning
Platform = Literal["youtube_shorts", "tiktok", "instagram_reels", "all"]


class YoutubeSubmitRequest(BaseModel):
    """POST /videos/youtube body."""
    url: HttpUrl
    goal: Goal = "viral"
    aspect_ratio: AspectRatio = Field(
        default="9:16",
        description="Output aspect ratio. 9:16 for Shorts/Reels, 1:1 for feed, 16:9 for YouTube.",
    )
    crop_position: CropPosition = Field(
        default="center",
        description="Which part of the source frame to keep when cropping.",
    )
    caption_preset: CaptionPreset = Field(
        default="hormozi",
        description=(
            "Caption style. 'hormozi' (Montserrat Bold + yellow highlight) is most viral. "
            "'bold' is clean white bold. 'minimal' is subtle lower-third. "
            "'tiktok' shows one big word per frame."
        ),
    )
    platform: Platform = Field(
        default="all",
        description=(
            "Target platform for clip selection tuning. "
            "'youtube_shorts' favors 45-75s structured clips. "
            "'tiktok' favors 20-45s punchy clips. "
            "'instagram_reels' favors 30-60s aesthetic clips. "
            "'all' balances across all platforms."
        ),
    )
    # Session C: post-production magic
    add_hook_outro: bool = Field(
        default=True,
        description=(
            "Prepend an AI-generated 3s pre-hook card and append a 2s outro loop card "
            "to each clip. Boosts retention dramatically (2026 best practice)."
        ),
    )
    remove_silences: bool = Field(
        default=True,
        description=(
            "Remove long pauses (>0.8s) from the clip before rendering. "
            "Tightens pacing, boosts completion rate. Safe: capped at 40% removal."
        ),
    )


class VideoJobOut(BaseModel):
    id: uuid.UUID
    source_type: str
    source_url: str | None
    status: str
    goal: str
    error_message: str | None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    completed_at: datetime | None
    model_config = ConfigDict(from_attributes=True)


class VideoJobListOut(BaseModel):
    jobs: list[VideoJobOut]
    total: int


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    message: str = "Job queued. Poll /videos/{job_id} for status."