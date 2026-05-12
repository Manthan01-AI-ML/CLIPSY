"""
backend/schemas/auth.py

Pydantic models for auth endpoints.
Step 11b: added newsletter_consent to RegisterRequest so we capture
marketing opt-in at signup.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, ConfigDict


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    """POST /auth/register body."""
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)

    # NEW Step 11b: marketing consent captured at signup.
    # Defaults to True (opt-in) but the UI shows it as a checkbox the user can uncheck.
    # When you set up email marketing later, query WHERE newsletter_consent=True.
    newsletter_consent: bool = Field(
        default=True,
        description="Whether user opted into promotional/marketing emails.",
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """POST /auth/login body."""
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Token response (used by login and refresh)
# ---------------------------------------------------------------------------
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


# ---------------------------------------------------------------------------
# User (returned on register, /users/me, etc.)
# ---------------------------------------------------------------------------
class UserOut(BaseModel):
    """Public-safe user fields. Never expose hashed_password or api_key."""
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    plan: str
    credits: int
    newsletter_consent: bool = True  # shows in /users/me for future settings UI
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)