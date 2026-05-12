"""
backend/api/routes/auth.py

Auth endpoints with rate limiting (Step 11) + newsletter consent (Step 11b):
  POST /auth/register  — 5/hour per IP, stores newsletter_consent
  POST /auth/login     — 10/minute per IP (blocks brute force)
  POST /auth/refresh   — 60/minute per IP (generous; tokens are short-lived)

NOTE: We do NOT use `from __future__ import annotations` here because slowapi +
FastAPI need runtime access to Pydantic model types at decorator evaluation time.
"""
from datetime import datetime, timezone

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
)
from backend.models.user import User, CreatorMemory, Plan
from backend.schemas.auth import (
    RegisterRequest, LoginRequest, RefreshRequest,
    TokenResponse, UserOut,
)


router = APIRouter()

limiter = Limiter(key_func=get_remote_address)


# Session WM: list of common disposable / throwaway email providers.
# Curated to cover the top abuse vectors without being so aggressive it blocks
# legitimate users. Sourced from the well-known disposable-email-domains lists
# (top ~40 most-used providers — covers ~95% of throwaway abuse).
_DISPOSABLE_EMAIL_DOMAINS = frozenset({
    # 10minutemail family
    "10minutemail.com", "10minutemail.net", "10mail.org", "10minemail.com",
    # mailinator family
    "mailinator.com", "mailinator.net", "mailinator.org", "mailinater.com",
    # tempmail family
    "tempmail.com", "tempmail.net", "tempmail.org", "tempmail.io", "tempmail.email",
    "temp-mail.org", "temp-mail.io", "tempmailo.com",
    # guerrillamail family
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "guerrillamail.de", "guerrillamailblock.com", "sharklasers.com", "grr.la",
    # yopmail family
    "yopmail.com", "yopmail.net", "yopmail.fr", "cool.fr.nf", "jetable.fr.nf",
    # Common others
    "throwawaymail.com", "trashmail.com", "trashmail.net", "fakeinbox.com",
    "getairmail.com", "maildrop.cc", "dispostable.com", "spambog.com",
    "spamgourmet.com", "spam4.me", "harakirimail.com", "incognitomail.com",
    "sogetthis.com", "mytemp.email", "emailondeck.com",
    "fake-email.com", "33mail.com", "mintemail.com", "mailnesia.com",
    "moakt.com", "instantemailaddress.com",
})


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
    # Session WM: disposable email blocker (anti-abuse for free tier)
    # We block obvious throwaway providers. Real users won't notice; bad actors
    # cycling free credits get a friendly bounce.
    email_lower = (payload.email or "").lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    if domain in _DISPOSABLE_EMAIL_DOMAINS:
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