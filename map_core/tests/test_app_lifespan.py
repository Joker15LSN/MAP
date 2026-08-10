"""Regression: the app lifespan must boot on FastAPI >= 0.141.

FastAPI 0.141 removed ``FastAPI.add_event_handler``. The previous
``setup_postgres`` / ``setup_mongodb`` implementations registered startup
and shutdown handlers through it, so boot crashed with::

    AttributeError: 'FastAPI' object has no attribute 'add_event_handler'

This test boots the *real* application lifespan with fake DB clients and
asserts that startup connectivity verification and shutdown cleanup are now
driven explicitly from the lifespan. It failed before the fix (AttributeError
on startup) and passes after.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import map_core.database.mongodb as mongodb_module
import map_core.database.postgre as postgre_module
from map_core.main import app


class _FakeDbClient:
    """Records lifecycle calls: verify_startup on boot, close on shutdown."""

    def __init__(self, *args, **kwargs) -> None:
        self.events: list[str] = []

    async def verify_startup(self) -> None:
        self.events.append("verify")

    async def close(self) -> None:
        self.events.append("close")


@pytest.fixture()
def _clear_app_state():
    # app is a module-level singleton; make sure no previous lifespan run
    # left clients behind (setup_* return the existing one if present).
    for attr in ("postgres_client", "mongodb_client"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
    yield
    for attr in ("postgres_client", "mongodb_client"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def test_app_lifespan_boots_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, _clear_app_state
) -> None:
    pg_instances: list[_FakeDbClient] = []
    mongo_instances: list[_FakeDbClient] = []

    def _pg_factory(*args, **kwargs) -> _FakeDbClient:
        client = _FakeDbClient()
        pg_instances.append(client)
        return client

    def _mongo_factory(*args, **kwargs) -> _FakeDbClient:
        client = _FakeDbClient()
        mongo_instances.append(client)
        return client

    monkeypatch.setattr(postgre_module, "PostgresClient", _pg_factory)
    monkeypatch.setattr(mongodb_module, "MongoClient", _mongo_factory)

    with TestClient(app) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200

        # Startup verification must have run for both stores.
        assert pg_instances and pg_instances[0].events == ["verify"]
        assert mongo_instances and mongo_instances[0].events == ["verify"]

    # Shutdown must close both clients exactly once.
    assert pg_instances[0].events == ["verify", "close"]
    assert mongo_instances[0].events == ["verify", "close"]
