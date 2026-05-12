"""
backend/core/config.py

Central configuration. Reads from .env.
Step 14: Sonnet 4.6 two-pass for best-in-class clip selection.
"""
from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All app settings. Validated by Pydantic on startup."""

    # --- App ---
    APP_NAME: str = "Clipsy"
    DEBUG: bool = False
    SECRET_KEY: str = "change_me_in_production"

    # --- Database ---
    DATABASE_URL: str = "postgresql://clipwise:clipwise_pw@db:5432/clipwise"

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- LLM Provider (Step 14: Sonnet primary, Haiku backup) ---
    # Sonnet 4.6 handles both pass 1 (full-video analysis) and pass 2 (deep scoring).
    # Haiku 4.5 is the fallback if Sonnet fails or budget is tight.
    LLM_PROVIDER: Literal["gemini", "groq", "claude", "mock"] = "claude"
    LLM_FALLBACK_PROVIDER: Literal["gemini", "groq", "claude", "mock", ""] = "groq"

    # Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # Groq — kept as fallback + used for Whisper
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # Claude (PRIMARY in Step 14: Sonnet 4.6)
    # Cost per MTok: $3 input / $15 output
    # Per-video cost (two-pass): ~$0.08 short, ~$0.12 for 1hr, ~$0.21 for 4hr
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-4-6"

    # --- Whisper transcription (Groq API primary, local fallback) ---
    WHISPER_PROVIDER: Literal["groq", "local"] = "groq"
    WHISPER_MODEL: Literal["tiny", "base", "small", "medium", "large-v3"] = "small"

    # --- Caption preset ---
    DEFAULT_CAPTION_PRESET: Literal["hormozi", "bold", "minimal", "tiktok"] = "hormozi"

    # --- Storage ---
    STORAGE_PATH: str = "/storage"
    MAX_UPLOAD_SIZE_MB: int = 500

    # --- Auth ---
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    # --- Free tier ---
    FREE_CREDITS: int = 3

    # --- Rate limits ---
    RATE_LIMIT_LOGIN: str = "10/minute"
    RATE_LIMIT_REGISTER: str = "5/hour"
    RATE_LIMIT_VIDEO_SUBMIT: str = "10/hour"
    RATE_LIMIT_DEFAULT: str = "100/minute"

    # --- Tier-1 security: CORS whitelist ---
    # Comma-separated list of origins allowed to call this API in production.
    # Example: CORS_ALLOWED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com
    # In DEBUG mode, localhost ports are auto-allowed; this var is optional.
    # In production (DEBUG=false), this MUST be set or the app will fail to start.
    # NOTE: kept separate from EXTRA_CORS_ORIGINS env var (which still works for
    # ngrok testing) — both lists are merged at startup.
    CORS_ALLOWED_ORIGINS: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()