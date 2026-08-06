"""Async SQLAlchemy engine and session factory for cbac_service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cbac_service.config import DATABASE_URL

# create_async_engine only builds the pool; no connection opens until first use.
engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=10)
_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def close_db() -> None:
    """Dispose of the engine connection pool. Call at app shutdown."""
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async session, closing it on exit."""
    async with _session_factory() as session:
        yield session
