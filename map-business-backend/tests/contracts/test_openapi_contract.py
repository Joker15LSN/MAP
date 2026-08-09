"""OpenAPI contract snapshot (FIX-P2-CONTRACT-E2E-01).

- every path/verb in the committed snapshot must still exist (no deletions,
  no field/type drift on legacy chat/admin routes);
- new /api/v1 and /internal/v1 errors use the standard envelope at runtime
  (FastAPI's default {"detail": ...} never leaks as a product error);
- regenerate the snapshot intentionally with:
    MAP_BFF_STATE_FILE=/tmp/x.json uv run python tests/contracts/gen_snapshot.py
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_contract_test_state.json")

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.settings import Settings

SNAPSHOT_PATH = Path(__file__).parent / "openapi_snapshot.json"

LEGACY_CHAT_PATHS = {
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream/v2"),
    ("POST", "/api/chat/flow/v1"),
    ("POST", "/api/chat/stream/flow/v1"),
}

LEGACY_ADMIN_PATHS = {
    ("GET", "/api/admin/full-config"),
    ("GET", "/api/admin/summary"),
    ("PUT", "/api/admin/model-center"),
    ("GET", "/api/admin/audit-logs"),
    ("PUT", "/api/admin/master-agent"),
}

NEW_API_PATHS = {
    ("POST", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations/{conversation_id}"),
    ("POST", "/api/v1/conversations/{conversation_id}/messages:stream"),
    ("POST", "/api/v1/messages/{message_id}:stop"),
    ("PUT", "/api/v1/messages/{message_id}/feedback"),
    ("DELETE", "/api/v1/messages/{message_id}/feedback"),
    ("GET", "/api/v1/admin/feedback"),
    ("GET", "/api/v1/admin/audit-events"),
    ("GET", "/api/v1/admin/audit-events/verify"),
    ("GET", "/internal/v1/ping"),
    ("GET", "/ready"),
    ("GET", "/health"),
}


def _all_operations(openapi: dict) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for path, methods in openapi["paths"].items():
        for method in ("get", "put", "post", "delete", "patch"):
            if method in methods:
                result.add((method.upper(), path))
    return result


def _app():
    return create_app(
        settings=Settings(auth_mode="dev", state_file="/tmp/map_bff_contract_test_state.json")
    )


def test_snapshot_has_no_deletions_and_no_unexpected_additions() -> None:
    """The committed snapshot is the contract: no silent deletions/additions."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = _all_operations(_app().openapi())
    snapshot_ops = _all_operations(snapshot)

    deleted = snapshot_ops - current
    assert not deleted, f"paths removed from OpenAPI contract: {sorted(deleted)}"

    added = current - snapshot_ops
    assert not added, (
        f"new OpenAPI operations must update the snapshot intentionally: {sorted(added)}"
    )


def test_legacy_chat_and_admin_routes_are_preserved() -> None:
    current = _all_operations(_app().openapi())
    assert current >= LEGACY_CHAT_PATHS
    assert current >= LEGACY_ADMIN_PATHS


def test_new_api_routes_are_present() -> None:
    current = _all_operations(_app().openapi())
    assert current >= NEW_API_PATHS


async def test_v1_errors_use_standard_envelope_not_fastapi_detail() -> None:
    """Runtime check: /api/v1 4xx bodies are {code,message,details,request_id}."""
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 404 conversation
        response = await client.get(f"/api/v1/conversations/{uuid.uuid4()}")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"code", "message", "details", "request_id"}
        assert "detail" not in body
        assert body["code"] == "RESOURCE_NOT_FOUND"
        assert body["request_id"]

        # 409 idempotency conflict on a real conversation
        created = await client.post(
            "/api/v1/conversations",
            json={"mode": "global"},
            headers={"Idempotency-Key": "e2e-conflict-key"},
        )
        assert created.status_code == 201
        conflict = await client.post(
            "/api/v1/conversations",
            json={"mode": "flow"},
            headers={"Idempotency-Key": "e2e-conflict-key"},
        )
        assert conflict.status_code == 409
        conflict_body = conflict.json()
        assert conflict_body["code"] == "IDEMPOTENCY_CONFLICT"
        assert set(conflict_body) == {"code", "message", "details", "request_id"}

        # 422 validation envelope (bad rating)
        response = await client.put(
            "/api/v1/messages/00000000-0000-0000-0000-000000000001/feedback",
            json={"rating": "great"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

        # internal service identity: browser token rejected with envelope
        internal = await client.get(
            "/internal/v1/ping",
            headers={
                "Authorization": "Bearer browser-token",
                "X-Service-Name": "browser",
                "X-Service-Audience": "map-bff",
                "X-Service-Scopes": "internal.ping",
            },
        )
        assert internal.status_code == 401
        assert internal.json()["code"] == "INVALID_SERVICE_IDENTITY"


async def test_legacy_error_shape_kept() -> None:
    """Legacy /api/* keeps FastAPI's {"detail": ...} shape (compat)."""
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/admin/nonexistent-route")
        assert response.status_code == 404
        body = response.json()
        assert "detail" in body and "code" not in body
