"""
backend/core/database.py

SQLAlchemy setup — engine, session factory, Base class.

Pattern: every request gets its own DB session via `get_db()` dependency.
Session auto-closes when request ends (prevents connection leaks).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from typing import Generator

from backend.core.config import settings


# Engine: connection pool to Postgres.
# pool_pre_ping=True  → checks connection is alive before using (avoids stale conns)
# echo=False          → set True to log all SQL (noisy, use for debugging only)
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
)

# Session factory. Each call to SessionLocal() = new session.
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# Base class all ORM models inherit from.
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency. Usage in routes:

        @router.get("/foo")
        def foo(db: Session = Depends(get_db)):
            ...

    Session is closed automatically when request finishes, even on error.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Create all tables. Called once on app startup.

    Note: For production, use Alembic migrations instead.
    This is fine for MVP / development.
    """
    # Import all models here so SQLAlchemy registers them with Base.
    # Even though we don't use these imports directly, they're needed
    # for Base.metadata to know about the tables.
    from backend.models import user, video, clip  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # Session WM: idempotent column add for lifetime_clips_generated.
    # SQLAlchemy's create_all does NOT add columns to existing tables, so we
    # patch the column in via DDL on every startup. Safe — Postgres' IF NOT
    # EXISTS makes this a no-op when the column is already present.
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users "
                "ADD COLUMN IF NOT EXISTS lifetime_clips_generated INTEGER NOT NULL DEFAULT 0"
            ))
    except Exception as e:
        # Don't fail startup if migration can't run (e.g. SQLite in tests).
        # The new column lives on the model regardless; only the bare-DB case
        # would fail at query time, and that's fine to log not crash.
        import logging
        logging.getLogger(__name__).warning(
            f"Could not add lifetime_clips_generated column "
            f"(might be SQLite or already exists): {e}"
        )