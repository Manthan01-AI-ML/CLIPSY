"""
backend/main.py

FastAPI app with:
  - Tier-1 security: CORS lockdown, security headers, log redaction
  - Rate limiting (slowapi) registered app-wide
  - Frontend SPA served at /
  - API routes for auth, users, videos, clips
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from backend.core.config import settings
from backend.core.database import init_db
from backend.core.security import apply_security
from backend.services.storage import ensure_storage_ready

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Rate limiter (shared across app)
# =============================================================================
# We create an app-level limiter. Individual routes can ALSO have their own
# @limiter.limit decorators (they do — see routes/auth.py and routes/videos.py).
# The app-level one is registered in app.state so slowapi's middleware finds it.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME} (debug={settings.DEBUG})")
    logger.info(
        f"LLM Provider: {settings.LLM_PROVIDER} "
        f"(model: {settings.GROQ_MODEL if settings.LLM_PROVIDER == 'groq' else '...'})"
    )
    logger.info(f"Whisper model: {settings.WHISPER_MODEL}")
    logger.info(f"Default caption preset: {settings.DEFAULT_CAPTION_PRESET}")
    try:
        init_db()
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"DB init failed: {e}")
    try:
        ensure_storage_ready()
        logger.info(f"Storage ready at {settings.STORAGE_PATH}")
    except Exception as e:
        logger.error(f"Storage not ready: {e}")

    yield
    logger.info("Shutting down.")


app = FastAPI(
    title=settings.APP_NAME,
    description="AI-powered video repurposing platform",
    version="0.1.0",
    lifespan=lifespan,
)

# --- Rate limiter wiring ---
# slowapi requires: store limiter in app.state, add middleware, register handler.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- Tier-1 security ---
# Adds: CORS lockdown (whitelist + EXTRA_CORS_ORIGINS env for ngrok),
#       security headers (X-Frame-Options, CSP, HSTS, etc.),
#       sensitive-data log redaction.
# Does NOT add a second rate limiter — slowapi above handles that.
apply_security(app)


# =============================================================================
# Health endpoint (JSON)
# =============================================================================
@app.get("/api/health", tags=["meta"])
def health():
    status_info = {"app": "ok"}
    try:
        from sqlalchemy import text
        from backend.core.database import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        status_info["db"] = "ok"
    except Exception as e:
        status_info["db"] = f"error: {type(e).__name__}"
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        r.ping()
        status_info["redis"] = "ok"
    except Exception as e:
        status_info["redis"] = f"error: {type(e).__name__}"
    status_info["llm_provider"] = settings.LLM_PROVIDER
    status_info["whisper_model"] = settings.WHISPER_MODEL
    return status_info


# =============================================================================
# API Routers — MUST come before the catch-all SPA route
# =============================================================================
from backend.api.routes import auth as auth_routes
from backend.api.routes import users as users_routes
from backend.api.routes import videos as videos_routes
from backend.api.routes import clips as clips_routes

app.include_router(auth_routes.router,   prefix="/auth",   tags=["auth"])
app.include_router(users_routes.router,  prefix="/users",  tags=["users"])
app.include_router(videos_routes.router, prefix="/videos", tags=["videos"])
app.include_router(clips_routes.router,  prefix="/clips",  tags=["clips"])


# =============================================================================
# Frontend SPA
# =============================================================================
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "templates" / "index.html"

if (FRONTEND_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")


@app.get("/", include_in_schema=False)
def serve_index():
    """Serve the dashboard SPA."""
    if INDEX_HTML.exists():
        return FileResponse(str(INDEX_HTML), media_type="text/html")
    return {"app": settings.APP_NAME, "version": "0.1.0", "docs": "/docs"}