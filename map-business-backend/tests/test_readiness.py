"""Review R-06 route-level tests: /ready never returns 200 when unready.

Negative scenarios (DSN unset / empty / malformed / refused / timeout) must
all return HTTP 503 with the fixed body shape, and the body must never leak
connection credentials. Positive scenarios run in the integration suite
(tests/integration/test_deploy_defaults.py) against the real database.
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import readiness as readiness_module
from app.main import create_app

CANARY_DSN = "postgresql+asyncpg://map:canary-password-xyz@127.0.0.1:1/map"


@pytest.fixture
def client():
    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _body(response) -> dict:
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def test_redact_dsn_strips_userinfo_and_query_password() -> None:
    assert readiness_module.redact_dsn(
        "postgresql+asyncpg://map:secret-1@host:5432/map"
    ) == "postgresql+asyncpg://map:<redacted>@host:5432/map"
    assert readiness_module.redact_dsn(
        "postgresql://h:5432/db?password=secret-2&x=1"
    ) == "postgresql://h:5432/db?password=<redacted>&x=1"


async def test_missing_dsn_returns_503_fixed_shape(client) -> None:
    os.environ.pop("MAP_CONTROL_DB_DSN", None)
    response = await client.get("/ready")
    assert response.status_code == 503
    body = _body(response)
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["database"]["error"] == "MAP_CONTROL_DB_DSN is not configured"


async def test_empty_dsn_returns_503(client, monkeypatch) -> None:
    monkeypatch.setenv("MAP_CONTROL_DB_DSN", "   ")
    response = await client.get("/ready")
    assert response.status_code == 503
    assert _body(response)["status"] == "not_ready"


async def test_malformed_dsn_returns_503(client, monkeypatch) -> None:
    monkeypatch.setenv("MAP_CONTROL_DB_DSN", "this-is-not-a-dsn")
    response = await client.get("/ready")
    assert response.status_code == 503
    assert _body(response)["status"] == "not_ready"


async def test_connection_refused_returns_503_without_leaking_credentials(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("MAP_CONTROL_DB_DSN", CANARY_DSN)
    response = await client.get("/ready")
    assert response.status_code == 503
    body = _body(response)
    assert body["checks"]["database"]["ok"] is False
    serialized = str(body)
    assert "canary-password-xyz" not in serialized


async def test_timeout_returns_503(client, monkeypatch) -> None:
    # TEST-NET-1 is a blackhole; a short connect timeout keeps the test fast.
    monkeypatch.setattr(readiness_module, "DB_CONNECT_TIMEOUT_SECONDS", 1)
    monkeypatch.setenv(
        "MAP_CONTROL_DB_DSN",
        "postgresql+asyncpg://map:pw@192.0.2.1:5432/map",
    )
    response = await client.get("/ready")
    assert response.status_code == 503
    body = _body(response)
    assert body["checks"]["database"]["ok"] is False
    assert "pw" not in str(body)
