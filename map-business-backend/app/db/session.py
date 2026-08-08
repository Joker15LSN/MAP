"""Async engine/session factory for the map_control schema.

DSN is taken from ``MAP_CONTROL_DB_DSN`` (defaults to the compose-local
PostgreSQL). Services control transactions; repositories never open their
own sessions.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DSN = "postgresql+asyncpg://map:map@127.0.0.1:15432/map"


def build_engine(dsn: str | None = None) -> AsyncEngine:
    return create_async_engine(
        dsn or os.getenv("MAP_CONTROL_DB_DSN", DEFAULT_DSN),
        pool_pre_ping=True,
    )


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide engine (created lazily)."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session; the caller owns the transaction."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
