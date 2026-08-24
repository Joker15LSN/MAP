"""R2-P2-04 runtime error matrix: every /api/v1 operation x error status.

The second-round review required parametrized runtime proof that ALL new
/api/v1 operations return the standard envelope for 401/403/404/409/422/500
(FastAPI's default {"detail": ...} must never leak on the new API).

- 401: trusted_header mode without proxy credentials (no DB needed);
- 403: trusted_header mode, valid credentials, non-privileged role — only
  the audit/admin operations are privilege-gated (conversation routes are
  owner-scoped and surface foreign resources as 404 instead);
- 404/409: real database (integration fixtures);
- 422: invalid bodies / malformed path UUIDs;
- 500: the DB session dependency is overridden to raise, proving the new
  generic exception handler wraps unexpected failures in the envelope.

The temp state file is an EXPLICIT test setting (passed to Settings), not
an environment impersonation of production defaults.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_error_matrix_state.json")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.settings import DEFAULT_WORKSPACE_CODE, DEFAULT_WORKSPACE_ID, Settings

pytestmark = pytest.mark.asyncio

STATE_FILE = "/tmp/map_bff_error_matrix_state.json"
SECRET = "matrix-secret-fake-1"
RANDOM_ID = str(uuid.uuid4())
ENVELOPE_KEYS = {"code", "message", "details", "request_id"}

BROWSER_OPERATIONS = [
    ("POST", "/api/v1/conversations", {"mode": "global"}),
    ("GET", "/api/v1/conversations", None),
    ("GET", f"/api/v1/conversations/{RANDOM_ID}", None),
    ("POST", f"/api/v1/conversations/{RANDOM_ID}/messages:stream", {"query": "hi"}),
    ("POST", f"/api/v1/messages/{RANDOM_ID}:stop", None),
    ("PUT", f"/api/v1/messages/{RANDOM_ID}/feedback", {"rating": "helpful"}),
    ("DELETE", f"/api/v1/messages/{RANDOM_ID}/feedback", None),
]

# Privilege-gated operations (require_audit_viewer): 403 is meaningful here.
ADMIN_OPERATIONS = [
    ("GET", "/api/v1/admin/feedback", None),
    ("GET", "/api/v1/admin/audit-events", None),
    ("GET", "/api/v1/admin/audit-events/verify", None),
]


def _trusted_app() -> object:
    return create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            trusted_proxy_secret=SECRET,
            trusted_proxy_required=True,
            state_file=STATE_FILE,
        )
    )


def _dev_app() -> object:
    return create_app(settings=Settings(auth_mode="dev", state_file=STATE_FILE))


def _assert_envelope(response, expected_code: str) -> None:
    body = response.json()
    assert set(body) == ENVELOPE_KEYS, f"envelope drift: {sorted(body)}"
    assert body["code"] == expected_code
    assert body["message"]
    assert body["request_id"]
    assert "detail" not in body


async def _request(client: AsyncClient, method: str, path: str, body, headers=None):
    kwargs: dict = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return await client.request(method, path, **kwargs)


# --- 401: every /api/v1 operation is auth-gated ------------------------------


@pytest.mark.parametrize("method,path,body", BROWSER_OPERATIONS + ADMIN_OPERATIONS)
async def test_401_without_credentials_uses_envelope(method, path, body) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_trusted_app()), base_url="http://test"
    ) as client:
        response = await _request(client, method, path, body)
        assert response.status_code == 401, response.text
        _assert_envelope(response, "AUTHENTICATION_REQUIRED")


# --- 403: valid credentials but missing privilege -----------------------------


@pytest.mark.parametrize("method,path,body", ADMIN_OPERATIONS)
async def test_403_member_role_on_admin_operations(method, path, body) -> None:
    headers = {
        "X-Trusted-Proxy-Secret": SECRET,
        "X-UserId": "u-member",
        "X-User-Roles": "member",
    }
    async with AsyncClient(
        transport=ASGITransport(app=_trusted_app()), base_url="http://test"
    ) as client:
        response = await _request(client, method, path, body, headers=headers)
        assert response.status_code == 403, response.text
        _assert_envelope(response, "FORBIDDEN")


# --- 404: missing resources on the new API ------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", f"/api/v1/conversations/{RANDOM_ID}", None),
        ("POST", f"/api/v1/conversations/{RANDOM_ID}/messages:stream", {"query": "hi"}),
        ("POST", f"/api/v1/messages/{RANDOM_ID}:stop", None),
        ("PUT", f"/api/v1/messages/{RANDOM_ID}/feedback", {"rating": "helpful"}),
        ("DELETE", f"/api/v1/messages/{RANDOM_ID}/feedback", None),
    ],
)
async def test_404_missing_resource_uses_envelope(_engine, method, path, body) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_dev_app()), base_url="http://test"
    ) as client:
        response = await _request(client, method, path, body)
        assert response.status_code == 404, response.text
        _assert_envelope(response, "RESOURCE_NOT_FOUND")


# --- 409: idempotency conflict -------------------------------------------------


async def test_409_idempotency_conflict_uses_envelope(_engine, session) -> None:
    await session.execute(
        text(
            "INSERT INTO map_control.workspaces (id, code, name, status) "
            "VALUES (:wid, :code, '默认工作空间', 'active')"
        ),
        {"wid": uuid.UUID(DEFAULT_WORKSPACE_ID), "code": DEFAULT_WORKSPACE_CODE},
    )
    await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=_dev_app()), base_url="http://test"
    ) as client:
        headers = {"Idempotency-Key": "matrix-conflict-key"}
        created = await client.post(
            "/api/v1/conversations", json={"mode": "global"}, headers=headers
        )
        assert created.status_code == 201, created.text
        conflict = await client.post(
            "/api/v1/conversations", json={"mode": "flow"}, headers=headers
        )
        assert conflict.status_code == 409, conflict.text
        _assert_envelope(conflict, "IDEMPOTENCY_CONFLICT")


# --- 422: validation failures --------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        # wrong type for mode (str field rejects a list)
        ("POST", "/api/v1/conversations", {"mode": ["global"]}),
        # empty query violates min_length=1
        ("POST", f"/api/v1/conversations/{RANDOM_ID}/messages:stream", {"query": ""}),
        # invalid rating pattern
        ("PUT", f"/api/v1/messages/{RANDOM_ID}/feedback", {"rating": "great"}),
        # malformed path UUID
        ("GET", "/api/v1/conversations/not-a-uuid", None),
        # malformed JSON body
        ("POST", "/api/v1/conversations", None),
    ],
)
async def test_422_validation_uses_envelope(method, path, body) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_dev_app()), base_url="http://test"
    ) as client:
        if method == "POST" and path == "/api/v1/conversations" and body is None:
            response = await client.post(
                path, content="{invalid-json", headers={"Content-Type": "application/json"}
            )
        else:
            response = await _request(client, method, path, body)
        assert response.status_code == 422, response.text
        _assert_envelope(response, "VALIDATION_ERROR")


# --- 500: unexpected failures wrap in the envelope ------------------------------


@pytest.mark.parametrize("method,path,body", BROWSER_OPERATIONS + ADMIN_OPERATIONS)
async def test_500_unexpected_failure_uses_envelope(method, path, body) -> None:
    app = _dev_app()

    async def _boom():
        raise RuntimeError("simulated datastore outage")
        yield  # pragma: no cover - makes this a generator dependency

    app.dependency_overrides[get_db_session] = _boom
    # ServerErrorMiddleware sends the envelope response and then re-raises
    # for server-side logging; ASGITransport defaults to propagating that
    # re-raise, which is not what we assert on here.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await _request(client, method, path, body)
        assert response.status_code == 500, response.text
        _assert_envelope(response, "INTERNAL_ERROR")
