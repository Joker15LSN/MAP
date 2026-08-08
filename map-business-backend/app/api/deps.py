"""Shared FastAPI dependencies.

F-01: routers receive ``store`` / ``core_client`` through ``app.state`` so
that ``create_app(store=..., core_client=...)`` overrides work for tests,
instead of closing over module-level singletons.
"""

from __future__ import annotations

from fastapi import Request

from ..core_client import MapCoreClient
from ..repositories.config import ConfigRepository


def get_store(request: Request) -> ConfigRepository:
    return request.app.state.store


def get_core_client(request: Request) -> MapCoreClient:
    return request.app.state.core_client
