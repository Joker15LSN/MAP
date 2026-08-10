
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

from fastapi import FastAPI, Request
from loguru import logger
from pymilvus import AsyncMilvusClient

from .. import config as app_config


class MilvusClient:
    """Async Milvus client wrapper for FastAPI dependencies."""

    def __init__(
        self,
        uri: str,
        *,
        user: str | None = None,
        password: str | None = None,
        token: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._token = token
        self._kwargs = kwargs
        self._client: AsyncMilvusClient | None = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    async def connect(self) -> AsyncMilvusClient:
        if self._client:
            return self._client
        if AsyncMilvusClient is None:
            raise ImportError("pymilvus is not installed.")

        logger.info("Initializing Milvus async client")
        client_args: dict[str, Any] = {"uri": self._uri}
        if self._user is not None:
            client_args["user"] = self._user
        if self._password is not None:
            client_args["password"] = self._password
        if self._token is not None:
            client_args["token"] = self._token

        # Merge user-supplied kwargs but drop None values to avoid API warnings.
        for key, value in self._kwargs.items():
            if value is not None:
                client_args[key] = value

        self._client = AsyncMilvusClient(**client_args)
        # Test connectivity once to fail fast if unreachable.
        await self._client.list_collections()
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
            logger.info("Milvus client closed")

    async def verify_startup(self) -> None:
        """Connect and list collections once so failures surface early."""
        milvus = await self.connect()
        try:
            await milvus.list_collections()
        except Exception as exc:  # noqa: BLE001 - surface runtime issue
            raise RuntimeError("Milvus connectivity check failed") from exc

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncMilvusClient]:
        if not self._client:
            await self.connect()

        assert self._client is not None
        yield self._client


def _resolve_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    cfg = config or getattr(app_config, "MILVUS_CONFIG", None)
    if not cfg or "uri" not in cfg:
        raise RuntimeError(
            "MILVUS_CONFIG is missing or does not contain a 'uri' entry."
        )
    return cfg


def setup_milvus(
    app: FastAPI, config: Mapping[str, Any] | None = None
) -> MilvusClient:
    """Attach a shared MilvusClient to FastAPI app.state and lifecycle."""

    existing = getattr(app.state, "milvus_client", None)
    if existing:
        return existing

    cfg = _resolve_config(config)
    client = MilvusClient(
        uri=cfg["uri"],
        user=cfg.get("user"),
        password=cfg.get("password"),
        token=cfg.get("token"),
        **{
            k: v
            for k, v in cfg.items()
            if k not in {"uri", "user", "password", "token"}
        },
    )

    app.state.milvus_client = client
    # NOTE: FastAPI >= 0.141 removed `add_event_handler`. setup_milvus is
    # only reached lazily from the request dependency, so the connection is
    # established there; no startup/shutdown handlers are registered.

    logger.info("MilvusClient setup complete")
    return client


async def get_milvus_client(request: Request) -> MilvusClient:
    """FastAPI dependency that returns the shared MilvusClient."""

    client: MilvusClient | None = getattr(request.app.state, "milvus_client", None)
    if client is None:
        client = setup_milvus(request.app)

    if not client.ready:
        await client.connect()
    return client


async def get_milvus_connection(
    request: Request,
) -> AsyncIterator[AsyncMilvusClient]:
    """FastAPI dependency that yields a Milvus client connection."""

    client = await get_milvus_client(request)
    async with client.connection() as conn:
        yield conn
