from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

from fastapi import FastAPI, Request
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .. import config as app_config


class MongoClient:
    """Motor async client wrapper for FastAPI dependencies."""

    def __init__(
        self,
        uri: str,
        *,
        database: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._uri = uri
        self._database = database
        self._kwargs = kwargs
        self._client: AsyncIOMotorClient | None = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    async def connect(self) -> AsyncIOMotorClient:
        if self._client:
            return self._client
        if AsyncIOMotorClient is None:
            raise ImportError("motor is not installed.")

        logger.info("Initializing MongoDB async client")
        self._client = AsyncIOMotorClient(self._uri, **self._kwargs)

        await self._client.admin.command("ping")
        return self._client

    async def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            logger.info("MongoDB client closed")

    async def verify_startup(self) -> bool:
        """Connect and ping once; report success instead of failing boot.

        Step 8 PR-K8: MongoDB is an optional boot dependency (only agent
        memory still uses it).  A missing config is handled by
        :func:`setup_mongodb`; a ping failure here is logged and reported as
        ``False`` so the caller can detach the client and continue booting.
        """
        try:
            mongo = await self.connect()
            await mongo.admin.command("ping")
            return True
        except Exception as exc:
            logger.warning("MongoDB connectivity check failed: {}", exc)
            await self.close()
            return False

    def get_database_name(self) -> str | None:
        return self._database

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncIOMotorClient]:
        if not self._client:
            await self.connect()

        assert self._client is not None
        yield self._client


def _resolve_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return a usable Mongo config or raise for strict callers.

    Strict callers are FastAPI dependencies that yield a database handle;
    they cannot degrade to a no-op response body.  Boot setup uses the
    optional resolver below instead.
    """
    cfg = _resolve_optional_config(config)
    if cfg is None:
        raise RuntimeError(
            "MONGODB_CONFIG is missing or does not contain a non-empty 'uri' entry."
        )
    return cfg


def _resolve_optional_config(
    config: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Return a usable Mongo config, or None when Mongo is not configured.

    An empty URI means "not configured" (P0-SEC-01: no repository default
    credentials), so boot and adapters must degrade instead of failing.
    """
    cfg = config or getattr(app_config, "MONGODB_CONFIG", None)
    if not cfg or "uri" not in cfg:
        return None
    if not str(cfg.get("uri") or "").strip():
        return None
    return cfg


def setup_mongodb(
    app: FastAPI, config: Mapping[str, Any] | None = None
) -> MongoClient | None:
    """Attach a shared MongoClient to FastAPI app.state, when configured.

    Step 8 PR-K8: Mongo is optional.  With no usable config the app must
    still boot; ``None`` is returned and the caller disables Mongo-backed
    adapters.
    """

    existing = getattr(app.state, "mongodb_client", None)
    if existing:
        return existing

    cfg = _resolve_optional_config(config)
    if cfg is None:
        logger.warning(
            "MONGODB_CONFIG missing or empty; Mongo-backed adapters disabled."
        )
        return None

    client = MongoClient(
        uri=cfg["uri"],
        database=cfg.get("database"),
        **{k: v for k, v in cfg.items() if k not in {"uri", "database"}},
    )

    app.state.mongodb_client = client
    # NOTE: FastAPI >= 0.141 removed `add_event_handler`; the app drives
    # connect/verify/close explicitly from its lifespan (see main.py).

    logger.info("MongoClient setup complete")
    return client


async def get_mongodb_client(request: Request) -> MongoClient | None:
    """FastAPI dependency that returns the shared MongoClient, if configured."""

    client: MongoClient | None = getattr(request.app.state, "mongodb_client", None)
    if client is None:
        client = setup_mongodb(request.app)

    if client is not None and not client.ready:
        await client.connect()
    return client


async def get_mongodb_connection(
    request: Request,
) -> AsyncIterator[AsyncIOMotorClient]:
    """FastAPI dependency that yields a MongoDB client connection."""

    client = await get_mongodb_client(request)
    if client is None:
        raise RuntimeError("MongoDB is not configured.")
    async with client.connection() as conn:
        yield conn


async def get_mongodb_database(
    request: Request,
) -> AsyncIterator[AsyncIOMotorDatabase]:
    """FastAPI dependency that yields a MongoDB database handle."""

    client = await get_mongodb_client(request)
    if client is None:
        raise RuntimeError("MongoDB is not configured.")
    db_name = client.get_database_name() or _resolve_config(None).get("database")
    if not db_name:
        raise RuntimeError("MONGODB_CONFIG is missing a 'database' entry.")

    async with client.connection() as conn:
        yield conn.get_database(db_name)
