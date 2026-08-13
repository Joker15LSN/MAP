"""Async engine/session factory for the map_control schema.

DSN is taken from ``MAP_CONTROL_DB_DSN``. P0-SEC-01: no repository
credential defaults — when the variable is unset the engine fails closed
(no DSN -> immediate connection error, never a guessed credential).
Compose always injects the variable; tests pass their own DSN.
Services control transactions; repositories never open their own
sessions.
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
from sqlalchemy.pool import NullPool

def build_engine(dsn: str | None = None) -> AsyncEngine:
    # NullPool: every checkout gets a fresh connection created on the
    # current event loop. The module-level singleton engine can therefore
    # be shared across loops (tests, reloads) without "Future attached to
    # a different loop" errors; the cost is a connect per request, which is
    # acceptable for this BFF's control-plane traffic.
    # P0-SEC-01: unset MAP_CONTROL_DB_DSN fails fast with a clear message
    # (no repository credential defaults, never a guessed DSN).
    resolved = dsn or os.getenv("MAP_CONTROL_DB_DSN", "")
    if not resolved:
        raise RuntimeError(
            "MAP_CONTROL_DB_DSN is not set: refusing to build a database engine "
            "without an explicitly injected DSN"
        )
    return create_async_engine(
        resolved,
        pool_pre_ping=True,
        poolclass=NullPool,
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
