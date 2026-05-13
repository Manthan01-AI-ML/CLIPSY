"""
backend/core/security.py

Combined module for:
  - Password hashing (bcrypt) — hash_password, verify_password
  - JWT tokens — create_access_token, create_refresh_token, decode_token, TokenError
  - Tier-1 security middleware — apply_security(app)

Per master doc Part 8 / Step 11 auth:
  - bcrypt rounds = 12
  - Access token: 15 min
  - Refresh token: 7 days
  - Algorithm: HS256

Tier-1 security middleware (apply_security):
  1. CORS lockdown — strict whitelist (production: from env, dev: localhost only)
  2. Security headers (Helmet equivalent) — X-Frame-Options, CSP, HSTS, etc.
  3. Sensitive-data log redaction — passwords, tokens, JWTs, API keys, emails

USAGE in main.py:
    from backend.core.security import apply_security
    apply_security(app)

Configuration via env vars (all optional):
  CORS_ALLOWED_ORIGINS    Comma-separated origins for production (required when DEBUG=false)
  EXTRA_CORS_ORIGINS      Your existing ngrok env var — still works, merged with above
  SECURITY_DISABLE        "true" disables ALL middleware (escape hatch)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware

from backend.core.config import settings

logger = logging.getLogger(__name__)


# =============================================================================
# Disposable email blocklist + is_email_allowed()
# =============================================================================
# Sourced from auth.py (Session 1: moved here so any module can import it).
# Covers the top ~60 most-used throwaway providers (~97% of throwaway abuse).
# Strategy: block-list only — any domain NOT listed is allowed. This means
# company domains like @eksum.co.in, @acme.com, etc. are always permitted.
#
# To add new providers: append to the frozenset below.
# To check: `from backend.core.security import is_email_allowed`
DISPOSABLE_EMAIL_DOMAINS: frozenset[str] = frozenset({
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
    # Common throwaway services
    "throwawaymail.com", "trashmail.com", "trashmail.net", "fakeinbox.com",
    "getairmail.com", "maildrop.cc", "dispostable.com", "spambog.com",
    "spamgourmet.com", "spam4.me", "harakirimail.com", "incognitomail.com",
    "sogetthis.com", "mytemp.email", "emailondeck.com",
    "fake-email.com", "33mail.com", "mintemail.com", "mailnesia.com",
    "moakt.com", "instantemailaddress.com",
    # Additional commonly-missed providers (Session 1 additions — 15 new)
    "getnada.com", "mailnull.com", "mailsac.com", "tempr.email",
    "discard.email", "spamgourmet.org", "trbvm.com", "wegwerfmail.de",
    "jetable.org", "mailzilla.org", "mailhazard.com", "trashmail.io",
    "notld.com", "inboxkitten.com", "spambog.ru",
})


def is_email_allowed(email: str) -> bool:
    """
    Return True if the email's domain is NOT on the disposable-email blocklist.

    Strategy: block-list only. Any domain not in DISPOSABLE_EMAIL_DOMAINS is
    allowed — including company domains (@eksum.co.in, @acme.in, etc.).

    Usage:
        if not is_email_allowed(payload.email):
            raise HTTPException(400, "Disposable email addresses are not supported.")
    """
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@")[-1]
    return domain not in DISPOSABLE_EMAIL_DOMAINS


# =============================================================================
# Password hashing (bcrypt — direct, no passlib)
# =============================================================================
# We use bcrypt directly instead of passlib because:
#   1. Modern bcrypt versions (>= 4.0) raise ValueError for passwords > 72 bytes
#      instead of silently truncating like passlib used to. Direct calls let
#      us truncate cleanly.
#   2. New bcrypt removed `bcrypt.__about__.__version__` which passlib relies on,
#      causing AttributeError noise at every hash/verify call.
#   3. Hashes produced here are 100% wire-compatible with passlib hashes (both
#      use the standard $2b$12$ bcrypt format), so existing user passwords in
#      the DB continue to verify without requiring resets.
#
# bcrypt rounds = 12 is the industry standard.
# bcrypt has a hard 72-byte password limit. We truncate explicitly.
import bcrypt as _bcrypt

_BCRYPT_ROUNDS = 12
_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    """Hash a plaintext password. Returns bcrypt hash like '$2b$12$...'."""
    if plain is None:
        plain = ""
    # bcrypt rejects > 72 bytes — truncate explicitly. (Passlib used to do this
    # silently; modern bcrypt raises ValueError instead.)
    pwd_bytes = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    salt = _bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return _bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Check if plaintext matches hashed password. Constant-time safe."""
    if plain is None or hashed is None:
        return False
    try:
        pwd_bytes = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return _bcrypt.checkpw(pwd_bytes, hashed.encode("utf-8"))
    except Exception:
        # Malformed hash or any other error → fail closed (not open)
        return False


# =============================================================================
# JWT tokens
# =============================================================================
class TokenError(Exception):
    """Raised when a token is invalid, expired, or tampered with."""


def _create_token(
    subject: str,
    expires_delta: timedelta,
    token_type: str,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Internal helper. Builds a JWT with standard claims."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),         # user id
        "iat": now,                  # issued at
        "exp": now + expires_delta,  # expires
        "type": token_type,          # 'access' or 'refresh'
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_access_token(user_id: str, extra_claims: dict | None = None) -> str:
    """Short-lived token used on every API request."""
    return _create_token(
        subject=user_id,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type="access",
        extra_claims=extra_claims,
    )


def create_refresh_token(user_id: str) -> str:
    """Long-lived token used only to get new access tokens."""
    return _create_token(
        subject=user_id,
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        token_type="refresh",
    )


def decode_token(token: str, expected_type: str = "access") -> dict:
    """
    Validate a JWT. Returns the payload.
    Raises TokenError if invalid/expired/wrong type.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except JWTError as e:
        raise TokenError(f"Invalid token: {e}") from e

    if payload.get("type") != expected_type:
        raise TokenError(
            f"Wrong token type: expected '{expected_type}', got '{payload.get('type')}'"
        )

    if "sub" not in payload:
        raise TokenError("Token missing subject")

    return payload


# =============================================================================
# Settings reader for middleware (env var fallback)
# =============================================================================
def _get_setting(name: str, default=None, cast=str):
    """Read setting from settings, fall back to env var."""
    if hasattr(settings, name):
        value = getattr(settings, name)
        if value is not None and value != "":
            return value
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    if cast is int:
        try:
            return int(raw)
        except ValueError:
            return default
    return cast(raw)


# =============================================================================
# Middleware 1: CORS lockdown
# =============================================================================
def _install_cors(app: FastAPI) -> None:
    extra_origins = [
        o.strip() for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()
    ]

    if settings.DEBUG:
        # Dev: explicit localhost list (NOT wildcard — wildcard with creds is CSRF vuln)
        origins = [
            "http://localhost",
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:8000",
            "http://localhost:8080",
            "http://127.0.0.1",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8080",
        ] + extra_origins
    else:
        # Production: must come from env
        raw = _get_setting("CORS_ALLOWED_ORIGINS", default="")
        if isinstance(raw, str):
            configured = [o.strip() for o in raw.split(",") if o.strip()]
        else:
            configured = list(raw)
        origins = configured + extra_origins
        if not origins:
            raise RuntimeError(
                "CORS_ALLOWED_ORIGINS env var is empty in production "
                "(DEBUG=false). Set it to your domain(s) before starting. "
                "Example: CORS_ALLOWED_ORIGINS=https://yourdomain.com"
            )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        max_age=600,
    )
    logger.info(f"[security] CORS configured: {len(origins)} allowed origin(s)")


# =============================================================================
# Middleware 2: Security headers (Helmet equivalent)
# =============================================================================
class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    DEFAULT_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Content-Security-Policy"] = self.DEFAULT_CSP
        proto = request.headers.get("x-forwarded-proto", "").lower()
        if proto == "https" or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


def _install_security_headers(app: FastAPI) -> None:
    app.add_middleware(_SecurityHeadersMiddleware)
    logger.info("[security] security headers middleware installed")


# =============================================================================
# Middleware 3: Sensitive-data log redaction
# =============================================================================
class _RedactingFormatter(logging.Formatter):
    """Redacts common sensitive patterns BEFORE log lines are emitted."""

    _PATTERNS: list[tuple[re.Pattern, str]] = [
        # JSON: "password": "value"
        (
            re.compile(
                r'("(?:password|passwd|pwd|secret|token|api_key|apikey|access_token|refresh_token)"\s*:\s*)"[^"]*"',
                re.I,
            ),
            r'\1"[REDACTED]"',
        ),
        # URL/form: password=value
        (
            re.compile(
                r'((?:password|passwd|pwd|secret|token|api_key|apikey|access_token|refresh_token)=)([^&\s"\']+)',
                re.I,
            ),
            r'\1[REDACTED]',
        ),
        # Bearer JWT
        (
            re.compile(
                r'(Bearer\s+)ey[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+',
                re.I,
            ),
            r'\1[REDACTED_JWT]',
        ),
        # Anthropic API keys (must come BEFORE OpenAI; prefix overlaps)
        (re.compile(r'sk-ant-[A-Za-z0-9_\-]{20,}'), 'sk-ant-[REDACTED]'),
        # OpenAI-style API keys
        (re.compile(r'sk-[A-Za-z0-9_\-]{20,}'), 'sk-[REDACTED]'),
        # AWS access key
        (re.compile(r'AKIA[0-9A-Z]{16}'), 'AKIA[REDACTED]'),
        # Google API key
        (re.compile(r'AIza[0-9A-Za-z\-_]{35}'), 'AIza[REDACTED]'),
        # Authorization header value
        (
            re.compile(r'(Authorization[":\s]+)([A-Za-z0-9_\-\.=]{20,})', re.I),
            r'\1[REDACTED]',
        ),
        # email in query string
        (re.compile(r'(\bemail=)([^&\s"\']+)', re.I), r'\1[REDACTED_EMAIL]'),
    ]

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return self._redact(original)

    @classmethod
    def _redact(cls, text: str) -> str:
        for pattern, replacement in cls._PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def _install_log_redaction() -> None:
    fmt_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    redacting = _RedactingFormatter(fmt_str)
    targets: list[logging.Logger] = [logging.getLogger()]
    for name in (
        "uvicorn", "uvicorn.access", "uvicorn.error",
        "fastapi", "celery", "celery.task", "celery.worker",
        "sqlalchemy",
    ):
        targets.append(logging.getLogger(name))

    for lg in targets:
        for h in lg.handlers:
            h.setFormatter(redacting)

    logger.info("[security] log redaction enabled")


# =============================================================================
# Public API: apply_security
# =============================================================================
def apply_security(app: FastAPI) -> None:
    """
    Apply Tier-1 security middleware to the FastAPI app.
    Call AFTER `app = FastAPI(...)`.

    Set SECURITY_DISABLE=true in env to disable everything (escape hatch).

    Note: This does NOT add a rate limiter. Your existing slowapi setup
    (with @limiter.limit decorators on auth/videos routes) handles that.
    """
    if _get_setting("SECURITY_DISABLE", default=False, cast=bool):
        logger.warning("[security] DISABLED via SECURITY_DISABLE=true")
        return

    _install_cors(app)
    _install_security_headers(app)
    _install_log_redaction()
    logger.info("[security] Tier-1 middleware installed")