"""S5-01 / S6-03: the worker->Core sandbox endpoint.

POST /sandbox/exec is a privileged command-execution surface:

- S6-03: authorization comes ONLY from a deployment-injected service
  credential (Bearer token, constant-time match, audience + sandbox:execute
  scope). Missing/forged/wrong-audience/wrong-scope credentials are
  rejected BEFORE any ledger write or any remote OpenSandbox byte, and the
  tests assert zero ledger rows and zero remote calls for each rejection;
- S5-01: a request missing ANY of the six durable identity fields fails
  closed with HTTP 400 (OPENSANDBOX_IDENTITY_INCOMPLETE) after auth;
- a complete authorized request drives the real tool chain end to end.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI

from map_core.routers.sandbox_router import IDENTITY_HEADERS, sandbox_router
from map_core.service.opensandbox_client import (
    IDEMPOTENCY_HEADER,
    OpenSandboxClient,
)
from map_core.service.sandbox_ledger import InMemorySandboxInvocationLedger
from map_core.service.sandbox_tools import (
    IDENTITY_INCOMPLETE,
    set_sandbox_ledger,
)

FULL_HEADERS = {
    "X-Workspace-ID": "ws-1",
    "X-Run-ID": "run-1",
    "X-Step-ID": "step-1",
    "X-Attempt-ID": "att-1",
    "X-Invocation-ID": "inv-1",
    "X-Client-Request-ID": "req-1",
}

TOKEN = "svc-sandbox-token"
CREDENTIALS = json.dumps(
    [
        {
            "key_id": "k-sandbox",
            "token": TOKEN,
            "service_name": "map-worker",
            "audience": "map-core",
            "scopes": ["sandbox:execute"],
        }
    ]
)

_LEDGER: InMemorySandboxInvocationLedger


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    global _LEDGER
    _LEDGER = InMemorySandboxInvocationLedger()
    set_sandbox_ledger(_LEDGER)
    monkeypatch.setenv("MAP_OPENSANDBOX_URL", "https://sandbox.test")
    monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_CREDENTIALS", CREDENTIALS)
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    yield
    set_sandbox_ledger(None)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(sandbox_router)
    return app


def _auth_headers(**overrides) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}", **FULL_HEADERS}
    headers.update(overrides)
    return headers


class FakeSandboxServer:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self.bytes_seen = 0
        self.sandboxes: dict[str, dict] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.bytes_seen += len(request.content)
        path = request.url.path
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method == "POST" and path == "/api/v1/sandboxes":
            payload = json.loads(request.content)
            self.create_calls.append({"key": key, "payload": payload})
            sandbox_id = "sb-1"
            self.sandboxes[payload.get("workspace_id")] = {
                "sandbox_id": sandbox_id,
                "status": "ready",
                "executions": [],
            }
            return httpx.Response(
                201, json={"sandbox_id": sandbox_id, "status": "ready"}
            )
        if request.method == "POST" and path.endswith("/execute"):
            payload = json.loads(request.content)
            executed = {
                "key": key,
                "command": payload.get("command"),
                "output": f"ok: {payload.get('command')}",
            }
            self.execute_calls.append({"key": key, "payload": payload})
            return httpx.Response(
                200,
                json={
                    "sandbox_id": "sb-1",
                    "status": "completed",
                    "output": executed["output"],
                },
            )
        if request.method == "GET" and "/api/v1/sandboxes/" in path:
            sandbox_id = path.rsplit("/", 1)[-1]
            for sandbox in self.sandboxes.values():
                if sandbox["sandbox_id"] == sandbox_id:
                    return httpx.Response(200, json=sandbox)
            return httpx.Response(404, json={"error": "unknown sandbox"})
        if request.method == "DELETE":
            for ws, sandbox in list(self.sandboxes.items()):
                if sandbox["sandbox_id"] == path.rsplit("/", 1)[-1]:
                    del self.sandboxes[ws]
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "no route"})


def _client(server: FakeSandboxServer) -> OpenSandboxClient:
    return OpenSandboxClient(
        base_url="https://sandbox.test",
        api_key="key-1234567890abcdef",
        transport=server.transport(),
    )


def _post(client, headers: dict) -> httpx.Response:
    return asyncio.run(
        client.post("/sandbox/exec", json={"command": "echo hi"}, headers=headers)
    )


class TestS6AuthBoundary:
    def test_no_authorization_is_401_with_zero_side_effects(self, monkeypatch) -> None:
        server = FakeSandboxServer()
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/sandbox/exec",
                        json={"command": "echo hi"},
                        headers=FULL_HEADERS,
                    )
            assert response.status_code == 401
            assert response.json()["error_code"] == "OPENSANDBOX_UNAUTHORIZED"

        asyncio.run(run())
        assert server.create_calls == []
        assert server.execute_calls == []
        assert server.bytes_seen == 0
        assert _LEDGER._rows == {}  # noqa: SLF001 - zero ledger writes

    def test_forged_token_is_401_with_zero_side_effects(self, monkeypatch) -> None:
        server = FakeSandboxServer()
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/sandbox/exec",
                        json={"command": "echo hi"},
                        headers=_auth_headers(Authorization="Bearer forged-token"),
                    )
            assert response.status_code == 401

        asyncio.run(run())
        assert server.bytes_seen == 0
        assert _LEDGER._rows == {}  # noqa: SLF001

    def test_wrong_audience_is_403_with_zero_side_effects(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "MAP_SANDBOX_SERVICE_CREDENTIALS",
            json.dumps(
                [
                    {
                        "key_id": "k-wrong",
                        "token": TOKEN,
                        "service_name": "map-worker",
                        "audience": "not-map-core",
                        "scopes": ["sandbox:execute"],
                    }
                ]
            ),
        )
        server = FakeSandboxServer()
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/sandbox/exec",
                        json={"command": "echo hi"},
                        headers=_auth_headers(),
                    )
            assert response.status_code == 403

        asyncio.run(run())
        assert server.bytes_seen == 0
        assert _LEDGER._rows == {}  # noqa: SLF001

    def test_wrong_scope_is_403_with_zero_side_effects(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "MAP_SANDBOX_SERVICE_CREDENTIALS",
            json.dumps(
                [
                    {
                        "key_id": "k-noscope",
                        "token": TOKEN,
                        "service_name": "map-worker",
                        "audience": "map-core",
                        "scopes": [],
                    }
                ]
            ),
        )
        server = FakeSandboxServer()
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/sandbox/exec",
                        json={"command": "echo hi"},
                        headers=_auth_headers(),
                    )
            assert response.status_code == 403

        asyncio.run(run())
        assert server.bytes_seen == 0
        assert _LEDGER._rows == {}  # noqa: SLF001

    def test_malformed_credential_registry_fails_closed_500(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_SANDBOX_SERVICE_CREDENTIALS", "{not-json")
        server = FakeSandboxServer()
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/sandbox/exec",
                        json={"command": "echo hi"},
                        headers=_auth_headers(),
                    )
            assert response.status_code == 500

        asyncio.run(run())
        assert server.bytes_seen == 0
        assert _LEDGER._rows == {}  # noqa: SLF001

    def test_missing_identity_fails_closed_with_400_after_auth(self) -> None:
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                for header in IDENTITY_HEADERS.values():
                    headers = {
                        k: v for k, v in _auth_headers().items() if k != header
                    }
                    response = await client.post(
                        "/sandbox/exec", json={"command": "echo hi"}, headers=headers
                    )
                    assert response.status_code == 400, header
                    body = response.json()
                    assert body["error_code"] == IDENTITY_INCOMPLETE

        asyncio.run(run())


class TestS5FullChain:
    def test_full_chain_carries_all_six_fields_into_the_tool(self, monkeypatch) -> None:
        server = FakeSandboxServer()
        transport = httpx.ASGITransport(app=_app())

        async def run() -> None:
            with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/sandbox/exec",
                        json={"command": "echo hi"},
                        headers=_auth_headers(),
                    )
            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert "ok: echo hi" in body["content"]
            assert len(server.create_calls) == 1
            assert len(server.execute_calls) == 1
            create_payload = server.create_calls[0]["payload"]
            for field, header in IDENTITY_HEADERS.items():
                assert create_payload.get(field) == FULL_HEADERS[header]
            execute_payload = server.execute_calls[0]["payload"]
            assert execute_payload["workspace_id"] == "ws-1"
            assert execute_payload["run_id"] == "run-1"
            assert execute_payload["step_id"] == "step-1"
            assert execute_payload["attempt_id"] == "att-1"
            assert execute_payload["invocation_id"] == "inv-1"
            assert execute_payload["client_request_id"] == "req-1"

        asyncio.run(run())
