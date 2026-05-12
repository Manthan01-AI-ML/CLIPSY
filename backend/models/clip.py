"""
backend/models/clip.py

Clip model — Step 13: editable captions support.

New columns:
  - original_transcript: JSONB — Whisper's word-level transcript for this clip
  - edited_transcript:   JSONB — user's corrected text (text-only, we keep original timestamps)
  - needs_rerender:      bool  — True when user saves edits but hasn't re-rendered yet
  - rerender_count:      int   — how many times this clip has been re-rendered (for analytics)
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, ForeignKey, Text, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.core.database import Base


class Clip(Base):
    __tablename__ = "clips"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_job_id = Column(
        UUID(as_uuid=True),
        ForeignKey("video_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    rank = Column(Integer, nullable=False)

    title = Column(String(255), nullable=True)
    hook = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)

    start_sec = Column(Float, nullable=False)
    end_sec = Column(Float, nullable=False)
    duration_sec = Column(Float, nullable=False)
    emotion = Column(String(50), nullable=True)

    virality_score = Column(Integer, nullable=True, default=50)

    # Step 12: detailed scoring breakdown + language + content_type
    meta = Column(JSONB, default=dict)

    # --- Step 13: Editable captions ---
    # Original transcript segments (from Whisper) for the clip's time range.
    # Schema: [{"start": float, "end": float, "text": str, "words": [...]}, ...]
    # Stored at creation, NEVER modified.
    original_transcript = Column(JSONB, nullable=True)

    # User's edited version. Text-only — we reuse the original timestamps
    # to avoid timing drift from edits.
    # Schema: [{"start": float, "end": float, "text": "user-edited text"}, ...]
    # NULL = user hasn't edited (use original)
    edited_transcript = Column(JSONB, nullable=True)

    # True when user has saved edits but we haven't re-rendered yet.
    # Set True on POST /clips/{id}/transcript
    # Set False when re-render completes successfully.
    needs_rerender = Column(Boolean, default=False, nullable=False)

    # How many times this clip has been re-rendered with user edits.
    # Useful for analytics: "users edit 40% of clips on average"
    rerender_count = Column(Integer, default=0, nullable=False)

    file_path = Column(String, nullable=True)
    thumbnail_path = Column(String, nullable=True)

    variant_of = Column(
        UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        nullable=True,
    )
    variant_type = Column(String, nullable=True)

    download_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    video_job = relationship("VideoJob", back_populates="clips")
    user = relationship("User", back_populates="clips")
    parent = relationship("Clip", remote_side=[id], backref="variants")

    def __repr__(self) -> str:
        return f"<Clip rank={self.rank} score={self.virality_score} {self.start_sec:.1f}-{self.end_sec:.1f}s>"