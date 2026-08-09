from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

import asyncpg
from asyncpg import Connection, Pool
from asyncpg.pool import PoolConnectionProxy
from fastapi import FastAPI, Request
from loguru import logger

from .. import config as app_config


class PostgresClient:
    """asyncpg pool wrapper for FastAPI dependencies."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 3,
        max_size: int = 50,
        timeout: float = 20.0,
        max_inactive_connection_lifetime: float = 90.0,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._max_inactive_connection_lifetime = max_inactive_connection_lifetime
        self._pool: Pool | None = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> Pool:
        """Create the pool lazily; safe to call multiple times."""
        if self._pool:
            return self._pool

        logger.info("Initializing PostgreSQL connection pool")
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._timeout,
            max_inactive_connection_lifetime=self._max_inactive_connection_lifetime,
        )
        return self._pool

    async def close(self) -> None:
        if not self._pool:
            return

        await self._pool.close()
        self._pool = None
        logger.info("PostgreSQL connection pool closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection | PoolConnectionProxy]:
        if not self._pool:
            await self.connect()

        assert self._pool is not None
        async with self._pool.acquire() as conn:
            yield conn


def _resolve_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    cfg = config or getattr(app_config, "POSTGRES_CONFIG", None)
    if not cfg or "dsn" not in cfg:
        raise RuntimeError(
            "POSTGRES_CONFIG is missing or does not contain a 'dsn' entry."
        )
    return cfg


def setup_postgres(
    app: FastAPI, config: Mapping[str, Any] | None = None
) -> PostgresClient:
    """Attach a shared PostgresClient to FastAPI app.state and lifecycle."""

    existing = getattr(app.state, "postgres_client", None)
    if existing:
        return existing

    cfg = _resolve_config(config)
    client = PostgresClient(
        dsn=cfg["dsn"],
        min_size=int(cfg.get("min_size", 3)),
        max_size=int(cfg.get("max_size", 50)),
        timeout=cfg.get("timeout", 20.0),
        max_inactive_connection_lifetime=cfg.get(
            "max_inactive_connection_lifetime", 90.0
        ),
    )

    app.state.postgres_client = client
    app.add_event_handler("startup", client.connect)
    # Verify connectivity once on startup so boot fails fast if the DB is unreachable.
    async def _verify_connection() -> None:
        pool = await client.connect()
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:
            raise RuntimeError("PostgreSQL connectivity check failed") from exc

    app.add_event_handler("startup", _verify_connection)
    app.add_event_handler("shutdown", client.close)



    logger.info("PostgresClient setup complete")

    return client


async def get_postgres_client(request: Request) -> PostgresClient:
    """FastAPI dependency that returns the shared PostgresClient."""

    client: PostgresClient | None = getattr(request.app.state, "postgres_client", None)
    if client is None:
        client = setup_postgres(request.app)

    if not client.ready:
        await client.connect()
    return client


async def get_postgres_connection(
    request: Request,
) -> AsyncIterator[Connection | PoolConnectionProxy]:
    """FastAPI dependency that yields a pooled connection."""

    client = await get_postgres_client(request)
    async with client.connection() as conn:
        yield conn
