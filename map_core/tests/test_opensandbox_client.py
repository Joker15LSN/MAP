"""OpenSandbox HTTP client contract tests (review R-02, P0-SEC-01).

Contract tests against a mock transport for everything the client controls
itself: auth header, durable identity fields, request idempotency key,
timeouts -> unknown-outcome reconciliation, typed errors and secret
redaction. The real-server integration (AC-SEC-12) remains blocked until
the OpenSandbox Server 0.2.2 is deployed; these tests guarantee the client
side of that contract is already correct.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from map_core.service.opensandbox_client import (
    API_ERROR,
    AUTH_HEADER,
    CONNECT_ERROR,
    IDEMPOTENCY_HEADER,
    MISSING_CONFIG_ERROR,
    UNKNOWN_OUTCOME,
    OpenSandboxClient,
    OpenSandboxClientError,
    SandboxIdentity,
    SandboxResourceLimits,
)

CANARY_KEY = "sandbox-canary-secret-0123456789"

IDENTITY = SandboxIdentity(
    workspace_id="11111111-1111-1111-1111-111111111111",
    run_id="22222222-2222-2222-2222-222222222222",
    step_id="33333333-3333-3333-3333-333333333333",
    attempt_id="44444444-4444-4444-4444-444444444444",
    invocation_id="55555555-5555-5555-5555-555555555555",
    client_request_id="req-0001",
)


def _client(handler) -> OpenSandboxClient:
    return OpenSandboxClient(
        base_url="http://sandbox.test:8080",
        api_key=CANARY_KEY,
        transport=httpx.MockTransport(handler),
    )


def _ok(response_body: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=response_body, request=request)

    return handler


def test_from_env_fails_closed_without_config(monkeypatch) -> None:
    monkeypatch.delenv("MAP_OPENSANDBOX_URL", raising=False)
    monkeypatch.delenv("MAP_OPENSANDBOX_API_KEY", raising=False)
    with pytest.raises(OpenSandboxClientError) as exc_info:
        OpenSandboxClient.from_env()
    assert exc_info.value.code == MISSING_CONFIG_ERROR


def test_create_sandbox_sends_auth_idempotency_and_identity() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201, json={"sandbox_id": "sb-1", "status": "running"}, request=request
        )

    client = _client(handler)
    result = asyncio.run(client.create_sandbox(IDENTITY))

    assert result["sandbox_id"] == "sb-1"
    # httpx normalizes header names to lowercase on the wire.
    assert seen["headers"][AUTH_HEADER.lower()] == CANARY_KEY
    assert seen["headers"][IDEMPOTENCY_HEADER.lower()] == IDENTITY.client_request_id
    for field in (
        "workspace_id",
        "run_id",
        "step_id",
        "attempt_id",
        "invocation_id",
        "client_request_id",
    ):
        assert seen["body"][field] == getattr(IDENTITY, field)
    assert set(seen["body"]["limits"]) == set(
        SandboxResourceLimits().to_dict()
    )


def test_retry_sends_same_idempotency_key() -> None:
    """A retried create must carry the same key so the server dedupes."""
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get(IDEMPOTENCY_HEADER))
        return httpx.Response(201, json={"sandbox_id": "sb-1"}, request=request)

    client = _client(handler)
    first = asyncio.run(client.create_sandbox(IDENTITY))
    second = asyncio.run(client.create_sandbox(IDENTITY))
    assert first["sandbox_id"] == second["sandbox_id"] == "sb-1"
    assert keys == [IDENTITY.client_request_id, IDENTITY.client_request_id]


def test_execute_timeout_reports_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client = _client(handler)
    with pytest.raises(OpenSandboxClientError) as exc_info:
        asyncio.run(client.execute("sb-1", IDENTITY, "echo hi"))
    assert exc_info.value.code == UNKNOWN_OUTCOME


def test_connection_failure_reports_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(OpenSandboxClientError) as exc_info:
        asyncio.run(client.health())
    assert exc_info.value.code == CONNECT_ERROR


def test_api_error_keeps_http_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=request)

    client = _client(handler)
    with pytest.raises(OpenSandboxClientError) as exc_info:
        asyncio.run(client.get_sandbox("sb-1"))
    assert exc_info.value.code == API_ERROR
    assert exc_info.value.status == 500


def test_reconcile_queries_remote_state() -> None:
    state = {"sandbox_id": "sb-1", "status": "succeeded"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/sandboxes/sb-1"
        return httpx.Response(200, json=state, request=request)

    client = _client(handler)
    snapshot = asyncio.run(client.reconcile("sb-1"))
    assert snapshot["status"] == "succeeded"


def test_destroy_returns_true_on_204() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    client = _client(handler)
    assert asyncio.run(client.destroy_sandbox("sb-1")) is True


def test_secret_never_leaks_in_repr_or_safe_headers() -> None:
    client = _client(lambda request: httpx.Response(200, json={}, request=request))
    assert CANARY_KEY not in repr(client)
    assert CANARY_KEY not in json.dumps(client.safe_headers())
    assert client.safe_headers()[AUTH_HEADER] == "<redacted>"
