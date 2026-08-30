"""Step 9 candidate 1: transport wire baselines before the adapter fold.

These tests pin the public wire contract that the runtime_transport adapter
must preserve:

- F-04 id validation through the REAL global-domain route (headers are
  applied by the router under test, captured through the request state);
- legacy SSE frame bytes exactly as the legacy SSE formatter renders them today;
- the two error JSON envelopes: FastAPI/Starlette ``{detail}`` and the
  execution router's ``{detail, error_code}``.

No test imports a private router helper: everything is driven through public
routes or public handlers.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from map_core.main import http_exception_handler
from map_core.routers import execution_router, global_domain_router
from map_core.schema.global_domain_schema import GlobalDomainStreamEvent

UUID4_HEX = re.compile(r"^[0-9a-f]{32}$")
TOKEN = "run-svc-token"
WORKSPACE_ID = "5a0ffc7e-9e9c-4a59-a3b4-7dffcd00e91a"


def _credential() -> dict:
    return {
        "key_id": "k-run",
        "token": TOKEN,
        "service_name": "map-bff",
        "audience": "map-core",
        "scopes": ["runs.execute"],
        "expires_at": "2099-12-31T23:59:59Z",
    }


class _FakeGlobalDomain:
    """Scripted GlobalDomain that captures the request state the router built."""

    captured: list[dict] = []

    def __init__(self, request=None, http_request=None) -> None:
        state = http_request.state
        self.captured.append(
            {
                "request_token": state.request_token,
                "x_userid": state.x_userid,
                "x_username": state.x_username,
                "request_id": state.request_id,
                "session_id": state.session_id,
                "workspace_id": state.workspace_id,
                "run_id": state.run_id,
                "attempt_id": state.attempt_id,
                "client_request_id": state.client_request_id,
            }
        )
        self.request_id = state.request_id
        self.state_id = state.session_id or "missing"

    def pipeline_stream(self, request):
        async def _gen():
            yield GlobalDomainStreamEvent(event="start", data={"context": "c"})
            yield GlobalDomainStreamEvent(event="content_delta", data={"delta": "hi"})
            yield GlobalDomainStreamEvent(event="done", data={"content": "hi"})

        return _gen()


@pytest.fixture()
def fake_global_domain(monkeypatch):
    _FakeGlobalDomain.captured = []
    monkeypatch.setattr(global_domain_router, "GlobalDomain", _FakeGlobalDomain)
    return _FakeGlobalDomain


def _global_stream_app() -> FastAPI:
    app = FastAPI()
    app.include_router(global_domain_router.global_domain_router)
    return app


async def _post_global_stream(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_global_stream_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/global_domain/chat/stream/v2",
            json={"query": "hello", "content_review_enabled": False},
            headers=headers,
        )


@pytest.fixture(autouse=True)
def _run_credentials(monkeypatch):
    monkeypatch.setenv("MAP_RUN_SERVICE_AUDIENCE", "map-core")
    monkeypatch.setenv(
        "MAP_RUN_SERVICE_CREDENTIALS", json.dumps([_credential()])
    )


def _make_request(headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": list((headers or {}).items()),
        "client": ("127.0.0.1", 12345),
        "server": ("test", 8000),
        "state": {},
        "app": None,
    }
    return Request(scope)


def _run_post_execution(run_id: uuid.UUID, attempt: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        app = FastAPI()
        app.include_router(execution_router.execution_router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.post(
                f"/internal/v1/runs/{run_id}/attempts/{attempt}/events",
                json={"query": "hello", "content_review_enabled": False},
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "X-Service-Name": "map-bff",
                    "X-Service-Audience": "map-core",
                    "X-Workspace-ID": WORKSPACE_ID,
                    "X-Request-ID": str(run_id),
                },
            )

    return asyncio.run(_run())


def test_id_contract_accepts_valid_ids_via_route(fake_global_domain) -> None:
    response = asyncio.run(
        _post_global_stream(
            {
                "X-Request-ID": "req-123.abc:def_1",
                "X-Session-ID": "sess_456.xyz:1",
                "X-Workspace-ID": "ws:team-7",
                "X-UserId": "user-1",
                "X-UserName": "zhang-san",
                "X-Run-ID": "run-9",
                "X-Attempt-ID": "att-2",
                "X-Client-Request-ID": "creq-3",
            }
        )
    )
    assert response.status_code == 200
    assert len(_FakeGlobalDomain.captured) == 1
    state = _FakeGlobalDomain.captured[0]
    assert state["request_id"] == "req-123.abc:def_1"
    assert state["session_id"] == "sess_456.xyz:1"
    assert state["workspace_id"] == "ws:team-7"
    assert state["x_userid"] == "user-1"
    assert state["x_username"] == "zhang-san"
    assert state["request_token"] is None
    assert state["run_id"] == "run-9"
    assert state["attempt_id"] == "att-2"
    assert state["client_request_id"] == "creq-3"


def test_id_contract_rejects_invalid_ids_via_route(fake_global_domain) -> None:
    response = asyncio.run(
        _post_global_stream(
            {
                "X-Request-ID": "bad id!@#$%^",
                "X-Session-ID": "x" * 200,
                "X-Workspace-ID": "workspace id with space",
                "X-UserId": "user-1",
                "X-UserName": "li-si",
            }
        )
    )
    assert response.status_code == 200
    assert len(_FakeGlobalDomain.captured) == 1
    state = _FakeGlobalDomain.captured[0]
    assert UUID4_HEX.fullmatch(state["request_id"]), state["request_id"]
    assert state["session_id"] is None
    assert state["workspace_id"] is None
    assert state["x_userid"] == "user-1"
    assert state["x_username"] == "li-si"


def test_id_contract_missing_ids_generate_request_id_only(
    fake_global_domain,
) -> None:
    response = asyncio.run(_post_global_stream({}))
    assert response.status_code == 200
    state = _FakeGlobalDomain.captured[0]
    assert UUID4_HEX.fullmatch(state["request_id"]), state["request_id"]
    assert state["session_id"] is None
    assert state["workspace_id"] is None


def test_legacy_sse_frame_bytes_via_route(fake_global_domain) -> None:
    response = asyncio.run(
        _post_global_stream({"X-Request-ID": "req-sse", "X-Workspace-ID": "ws-1"})
    )
    assert response.status_code == 200
    assert response.text == (
        "event: start\n"
        'data: {"context": "c"}\n'
        "\n"
        "event: content_delta\n"
        'data: {"delta": "hi"}\n'
        "\n"
        "event: done\n"
        'data: {"content": "hi"}\n'
        "\n"
    )


def test_execution_error_json_has_detail_and_error_code() -> None:
    run_id = uuid.uuid4()
    response = _run_post_execution(run_id, "not-an-int")
    assert response.status_code == 400
    assert response.json() == {
        "detail": "attempt path parameter must be an integer >= 1",
        "error_code": "RUNS_EXECUTE_INVALID_REQUEST",
    }


def test_http_exception_detail_only_envelope() -> None:
    response = asyncio.run(
        http_exception_handler(
            _make_request(),
            HTTPException(status_code=404, detail="nope"),
        )
    )
    assert response.status_code == 404
    assert json.loads(response.body) == {"detail": "nope"}
