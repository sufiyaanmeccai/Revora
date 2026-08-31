"""
app/core/database.py
--------------------
Async SQLAlchemy database layer for Revora.

Provides:
  • Async engine backed by aiosqlite.
  • Sessionmaker factory returning AsyncSession.
  • FastAPI dependency get_db() for per-request session injection.
  • init_db() to create all ORM-mapped tables on startup.
"""

import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,          # SQL query logging in debug mode
    future=True,
    connect_args={"check_same_thread": False},  # SQLite-specific
)

# --------------------------------------------------------------------------- #
# Session factory                                                              #
# --------------------------------------------------------------------------- #
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,       # Keep ORM objects accessible post-commit
    autoflush=False,
    autocommit=False,
)


# --------------------------------------------------------------------------- #
# FastAPI dependency                                                           #
# --------------------------------------------------------------------------- #
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async database session for use in FastAPI route dependencies.

    The session is automatically closed (and rolled back on exception) when the
    request context exits.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# --------------------------------------------------------------------------- #
# Table initialisation                                                         #
# --------------------------------------------------------------------------- #
async def init_db() -> None:
    """
    Create all SQLAlchemy-mapped tables in the database.

    Called once during application startup via the FastAPI lifespan handler.
    Safe to call on each startup — existing tables are not modified (checkfirst
    is the default behaviour of metadata.create_all).
    """
    # Import here to ensure all ORM models are registered before create_all
    from app.models import orm  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(orm.Base.metadata.create_all)

    logger.info("✅  Database tables initialised.")
