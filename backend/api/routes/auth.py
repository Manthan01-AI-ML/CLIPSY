"""
backend/api/routes/auth.py

Auth endpoints with rate limiting (Step 11) + newsletter consent (Step 11b):
  POST /auth/register        — 5/hour per IP, stores newsletter_consent
  POST /auth/login           — 10/minute per IP (blocks brute force)
  POST /auth/refresh         — 60/minute per IP (generous; tokens are short-lived)
  POST /auth/forgot-password — 3/hour per IP (Session 1)
  POST /auth/reset-password  — 5/hour per IP (Session 1)

NOTE: We do NOT use `from __future__ import annotations` here because slowapi +
FastAPI need runtime access to Pydantic model types at decorator evaluation time.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.core.database import get_db
from backend.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, TokenError,
    is_email_allowed,
)
from backend.models.password_reset import PasswordResetToken
from backend.models.user import User, CreatorMemory, Plan
from backend.schemas.auth import (
    RegisterRequest, LoginRequest, RefreshRequest,
    TokenResponse, UserOut,
    ForgotPasswordRequest, ResetPasswordRequest,
)


router = APIRouter()

limiter = Limiter(key_func=get_remote_address)


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user account",
)
@limiter.limit(settings.RATE_LIMIT_REGISTER)  # 5/hour default
def register(
    request: Request,
    payload: RegisterRequest,
    db: Session = Depends(get_db),
) -> UserOut:
    if not is_email_allowed(payload.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Please use a real email address. Disposable email providers "
                "are not supported. (We email you when your clips are ready.)"
            ),
        )

    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        plan=Plan.free,
        credits=settings.FREE_CREDITS,
        # NEW Step 11b: store marketing opt-in
        newsletter_consent=payload.newsletter_consent,
        newsletter_consent_at=(
            datetime.now(timezone.utc) if payload.newsletter_consent else None
        ),
    )
    db.add(user)
    db.flush()

    memory = CreatorMemory(user_id=user.id)
    db.add(memory)

    db.commit()
    db.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email + password for JWT tokens",
)
@limiter.limit(settings.RATE_LIMIT_LOGIN)  # 10/minute default
def login(
    request: Request,
    payload: LoginRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    user = db.query(User).filter(User.email == payload.email).first()

    # Timing-safe: always verify password even if user doesn't exist.
    is_valid = user is not None and verify_password(payload.password, user.hashed_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    user_id = str(user.id)
    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Get a new access token using a refresh token",
)
@limiter.limit("60/minute")
def refresh(
    request: Request,
    payload: RefreshRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    try:
        data = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )

    user_id = data["sub"]

    import uuid as _uuid
    user = db.query(User).filter(User.id == _uuid.UUID(user_id)).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
        )

    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


# =============================================================================
# Session 1: Forgot password / reset password
# =============================================================================

_TOKEN_EXPIRY_MINUTES = 15
_RATE_LIMIT_FORGOT = "3/hour"
_RATE_LIMIT_RESET = "5/hour"


@router.post(
    "/forgot-password",
    status_code=status.HTTP_200_OK,
    summary="Request a password-reset link via email",
)
@limiter.limit(_RATE_LIMIT_FORGOT)
def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> dict:
    """
    Always returns 200 with a generic message — prevents email enumeration.
    If the email exists: generates a token, stores its SHA-256 hash, sends email.
    If the email doesn't exist: does nothing but returns the same response.

    Token security:
      - Raw token: secrets.token_urlsafe(32) — 256 bits of entropy
      - Stored in DB: SHA-256(raw_token) only — never the raw value
      - Expires in 15 minutes
      - Any previous unused tokens for this user are invalidated first
    """
    _GENERIC_OK = {"message": "If that email is registered, a reset link is on its way."}

    user = db.query(User).filter(User.email == payload.email).first()
    if user is None:
        return _GENERIC_OK

    # Invalidate any previous unexpired tokens for this user (one valid token at a time)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.expires_at > datetime.now(timezone.utc),
    ).delete(synchronize_session=False)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=_TOKEN_EXPIRY_MINUTES)

    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(reset_token)
    db.commit()

    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"

    try:
        from backend.services.notifications import send_password_reset_email
        send_password_reset_email(
            to_email=user.email,
            reset_url=reset_url,
            user_name=user.full_name,
        )
    except Exception:
        # Email failure must not reveal whether the address exists — swallow it.
        # The error is already logged inside send_password_reset_email.
        pass

    return _GENERIC_OK


@router.post(
    "/reset-password",
    status_code=status.HTTP_200_OK,
    summary="Set a new password using a reset token",
)
@limiter.limit(_RATE_LIMIT_RESET)
def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> dict:
    """
    Validates the token (not expired, not used), updates the user's password,
    marks the token as consumed. Returns 400 for any invalid / expired token
    without revealing which specific check failed (prevents oracle attacks).
    """
    _INVALID = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This reset link is invalid or has expired. Please request a new one.",
    )

    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()

    reset_token = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == token_hash)
        .first()
    )

    if reset_token is None:
        raise _INVALID
    if reset_token.used_at is not None:
        raise _INVALID
    if reset_token.expires_at < datetime.now(timezone.utc):
        raise _INVALID

    user = db.query(User).filter(User.id == reset_token.user_id).first()
    if user is None:
        raise _INVALID

    user.hashed_password = hash_password(payload.new_password)
    reset_token.used_at = datetime.now(timezone.utc)
    db.commit()

    return {"message": "Password updated successfully. You can now log in."}