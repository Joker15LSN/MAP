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

    async def verify_startup(self) -> None:
        """Connect and ping once so boot fails fast."""
        mongo = await self.connect()
        try:
            await mongo.admin.command("ping")
        except Exception as exc:
            raise RuntimeError("MongoDB connectivity check failed") from exc

    def get_database_name(self) -> str | None:
        return self._database

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncIOMotorClient]:
        if not self._client:
            await self.connect()

        assert self._client is not None
        yield self._client


def _resolve_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    cfg = config or getattr(app_config, "MONGODB_CONFIG", None)
    if not cfg or "uri" not in cfg:
        raise RuntimeError(
            "MONGODB_CONFIG is missing or does not contain a 'uri' entry."
        )
    return cfg


def setup_mongodb(app: FastAPI, config: Mapping[str, Any] | None = None) -> MongoClient:
    """Attach a shared MongoClient to FastAPI app.state and lifecycle."""

    existing = getattr(app.state, "mongodb_client", None)
    if existing:
        return existing

    cfg = _resolve_config(config)
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


async def get_mongodb_client(request: Request) -> MongoClient:
    """FastAPI dependency that returns the shared MongoClient."""

    client: MongoClient | None = getattr(request.app.state, "mongodb_client", None)
    if client is None:
        client = setup_mongodb(request.app)

    if not client.ready:
        await client.connect()
    return client


async def get_mongodb_connection(
    request: Request,
) -> AsyncIterator[AsyncIOMotorClient]:
    """FastAPI dependency that yields a MongoDB client connection."""

    client = await get_mongodb_client(request)
    async with client.connection() as conn:
        yield conn


async def get_mongodb_database(
    request: Request,
) -> AsyncIterator[AsyncIOMotorDatabase]:
    """FastAPI dependency that yields a MongoDB database handle."""

    client = await get_mongodb_client(request)
    db_name = client.get_database_name() or _resolve_config(None).get("database")
    if not db_name:
        raise RuntimeError("MONGODB_CONFIG is missing a 'database' entry.")

    async with client.connection() as conn:
        yield conn.get_database(db_name)
