"""
backend/models/video.py

VideoJob model — represents one video being processed.
Per master doc Part 4 and Part 5 (pipeline stages).
"""
from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Column, String, DateTime, ForeignKey, Enum, Text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.core.database import Base


class SourceType(str, enum.Enum):
    youtube = "youtube"
    upload = "upload"


class JobStatus(str, enum.Enum):
    """Pipeline stages in order (master doc Part 5)."""
    queued = "queued"
    downloading = "downloading"
    transcribing = "transcribing"
    scoring = "scoring"
    rendering = "rendering"
    done = "done"
    failed = "failed"


class Goal(str, enum.Enum):
    """Clipping goal (master doc Part 1 — differentiator from competitors)."""
    viral = "viral"
    authority = "authority"
    lead_gen = "lead_gen"
    educational = "educational"


class VideoJob(Base):
    __tablename__ = "video_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_type = Column(Enum(SourceType, name="source_type_enum"), nullable=False)
    source_url = Column(String, nullable=True)     # YouTube URL
    file_path = Column(String, nullable=True)      # uploaded file path

    status = Column(
        Enum(JobStatus, name="job_status_enum"),
        default=JobStatus.queued,
        nullable=False,
        index=True,
    )
    goal = Column(
        Enum(Goal, name="goal_enum"),
        default=Goal.viral,
        nullable=False,
    )

    error_message = Column(Text, nullable=True)
    meta = Column(JSONB, default=dict)  # duration, language, title, etc.

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="video_jobs")
    clips = relationship(
        "Clip",
        back_populates="video_job",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<VideoJob {self.id} status={self.status.value}>"