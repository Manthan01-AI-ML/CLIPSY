"""
backend/models/password_reset.py

PasswordResetToken — short-lived, single-use tokens for the forgot-password flow.

Security properties:
  - Raw token is generated with secrets.token_urlsafe(32) — 256 bits of entropy.
  - Only the SHA-256 hash is stored here. Raw token is never persisted or logged.
  - expires_at: 15 minutes from creation (enforced at validation time in auth.py).
  - used_at: set when the token is consumed. Any token with used_at != None is rejected.
  - One valid token per user enforced at the application layer (old tokens are
    invalidated when a new request is made for the same email).
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.core.database import Base


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 hex digest of the raw token. Never store raw token.
    token_hash = Column(String(64), nullable=False, unique=True, index=True)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")

    def __repr__(self) -> str:
        return f"<PasswordResetToken user_id={self.user_id} used={self.used_at is not None}>"
