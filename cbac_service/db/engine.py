"""Async SQLAlchemy engine and session factory for cbac_service."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cbac_service.config import DATABASE_URL

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Create the async engine and session factory. Call once at app startup."""
    global _engine, _session_factory

    if _engine is not None:
        return

    _engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=5,
        max_overflow=10,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def close_db() -> None:
    """Dispose of the engine connection pool. Call at app shutdown."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session. Use as a FastAPI dependency or async context manager."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")

    async with _session_factory() as session:
        yield session
