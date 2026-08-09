"""F-04 acceptance: RequestPrincipal, admin gate, request/session/workspace IDs.

Dev mode yields a fixed local administrator; trusted_header mode parses
proxy-supplied identity (and requires the shared secret when enabled); the
request context middleware owns request_id/session_id/workspace_id and
forwards them to map_core.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_principal_test_state.json")

from app.core.identity import AuthMode, is_valid_id
from app.main import create_app
from app.settings import Settings


class FakeCoreClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, payload, headers):
        self.calls.append(headers)
        return {"content": "ok", "meta": {}}

    async def chat_by_path(self, path, payload, headers):
        self.calls.append(headers)
        return {"content": "ok", "meta": {}}

    async def stream_chat(self, payload, headers):
        yield b'event: done\ndata: {"content":"ok"}\n\n'

    async def stream_chat_by_path(self, path, payload, headers):
        yield b'event: done\ndata: {"content":"ok"}\n\n'


def _app(settings: Settings):
    from dataclasses import replace

    settings = replace(settings, state_file="/tmp/map_bff_principal_state.json")
    return create_app(
        settings=settings,
        store=None,
        core_client=FakeCoreClient(),
    )


def test_dev_mode_default_admin_can_write_admin_config() -> None:
    app = _app(Settings(auth_mode=AuthMode.DEV))
    client = TestClient(app)
    response = client.put("/api/admin/model-center", json={"large_models": []})
    assert response.status_code == 200


def test_trusted_header_mode_parses_principal_and_roles() -> None:
    app = _app(Settings(auth_mode=AuthMode.TRUSTED_HEADER, trusted_proxy_secret="s3cret", trusted_proxy_required=True))
    client = TestClient(app)
    response = client.put(
        "/api/admin/model-center",
        headers={
            "X-UserId": "u-1",
            "X-UserName": "Zhang San",
            "X-User-Roles": "platform_admin,evaluator",
            "X-User-Department": "R&D",
            "X-Workspace-ID": "ws-1",
            "X-Trusted-Proxy-Secret": "s3cret",
        },
        json={"large_models": []},
    )
    assert response.status_code == 200


def test_trusted_header_without_admin_role_is_forbidden() -> None:
    app = _app(Settings(auth_mode=AuthMode.TRUSTED_HEADER, trusted_proxy_secret="s3cret", trusted_proxy_required=True))
    client = TestClient(app)
    response = client.put(
        "/api/admin/model-center",
        headers={"X-UserId": "u-2", "X-User-Roles": "member", "X-Trusted-Proxy-Secret": "s3cret"},
        json={"large_models": []},
    )
    assert response.status_code == 403


def test_trusted_header_requires_secret_when_enabled() -> None:
    app = _app(
        Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            trusted_proxy_required=True,
            trusted_proxy_secret="s3cret",
        )
    )
    client = TestClient(app)
    # Missing secret -> 401.
    response = client.get("/api/admin/summary", headers={"X-UserId": "u-1"})
    assert response.status_code == 401
    # Wrong secret -> 401.
    response = client.get(
        "/api/admin/summary",
        headers={"X-UserId": "u-1", "X-Trusted-Proxy-Secret": "wrong"},
    )
    assert response.status_code == 401
    # Correct secret -> 200.
    response = client.get(
        "/api/admin/summary",
        headers={"X-UserId": "u-1", "X-Trusted-Proxy-Secret": "s3cret"},
    )
    assert response.status_code == 200


def test_trusted_header_missing_user_is_401() -> None:
    app = _app(Settings(auth_mode=AuthMode.TRUSTED_HEADER, trusted_proxy_secret="s3cret", trusted_proxy_required=True))
    client = TestClient(app)
    response = client.get("/api/admin/summary")
    assert response.status_code == 401


def test_oidc_mode_fails_closed() -> None:
    app = _app(Settings(auth_mode=AuthMode.OIDC))
    client = TestClient(app)
    response = client.get("/api/admin/summary")
    assert response.status_code == 501


def test_request_id_echoed_and_session_workspace_forwarded(monkeypatch) -> None:
    core = FakeCoreClient()
    app = create_app(
        settings=Settings(auth_mode=AuthMode.DEV, state_file="/tmp/map_bff_principal_state.json"),
        store=None,
        core_client=core,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"query": "hi"},
        headers={"X-Request-ID": "req-abc123", "X-Session-ID": "sess-xyz"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-abc123"

    forwarded = core.calls[0]
    assert forwarded["X-Request-ID"] == "req-abc123"
    assert forwarded["X-Session-ID"] == "sess-xyz"
    assert forwarded["X-Workspace-ID"] == "00000000-0000-0000-0000-000000000001"  # default workspace


def test_invalid_request_id_is_replaced_with_fresh_one() -> None:
    core = FakeCoreClient()
    app = create_app(
        settings=Settings(auth_mode=AuthMode.DEV, state_file="/tmp/map_bff_principal_state.json"),
        store=None,
        core_client=core,
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={"query": "hi"},
        headers={"X-Request-ID": "!!!bad id with spaces!!!"},
    )
    assert response.status_code == 200
    echoed = response.headers["X-Request-ID"]
    assert is_valid_id(echoed)
    assert echoed != "!!!bad id with spaces!!!"
    assert core.calls[0]["X-Request-ID"] == echoed


def test_missing_request_id_mints_one_and_echoes() -> None:
    core = FakeCoreClient()
    app = create_app(
        settings=Settings(auth_mode=AuthMode.DEV, state_file="/tmp/map_bff_principal_state.json"),
        store=None,
        core_client=core,
    )
    client = TestClient(app)
    response = client.post("/api/chat", json={"query": "hi"})
    assert response.status_code == 200
    echoed = response.headers["X-Request-ID"]
    assert is_valid_id(echoed)
    assert core.calls[0]["X-Request-ID"] == echoed


def test_dev_mode_startup_fails_in_production(monkeypatch) -> None:
    monkeypatch.setenv("MAP_ENV", "prod")
    with pytest.raises(RuntimeError, match="MAP_AUTH_MODE=dev is forbidden"):
        create_app(
            settings=Settings(
                auth_mode=AuthMode.DEV, state_file="/tmp/map_bff_principal_state.json"
            )
        )
