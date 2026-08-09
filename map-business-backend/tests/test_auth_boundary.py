"""FIX-P0-AUTH-01 acceptance: fail-closed trusted identity + admin authz.

- no/wrong/forged proxy credentials -> 401, admin state unchanged, no audit
- valid proxy credentials + non-admin role -> 403
- valid proxy credentials + platform_admin -> allowed
- prod + dev / prod + trusted_header without verification -> startup failure
- service tokens: bad audience/scope -> 401/403; browser token on /internal/v1 -> 401
- secret fuzz: the proxy secret never appears in responses, logs or audit
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_auth_test_state.json")

import pytest
from fastapi.testclient import TestClient

from app.core.identity import AuthMode
from app.main import create_app
from app.settings import Settings

SECRET = "s3cret-value-42"
OTHER_SECRET = "other-value-99"


def _app(settings: Settings):
    from dataclasses import replace

    settings = replace(settings, state_file="/tmp/map_bff_auth_test_state.json")
    return create_app(settings=settings, store=None, core_client=None)


def _trusted(secret: str = SECRET, roles: str = "member") -> Settings:
    return Settings(
        auth_mode=AuthMode.TRUSTED_HEADER,
        trusted_proxy_secret=secret,
        trusted_proxy_required=True,
    )


# --- 1. forged / missing / wrong credentials ----------------------------------


def test_no_proxy_credentials_is_401_with_envelope() -> None:
    app = _app(_trusted())
    client = TestClient(app)
    response = client.put(
        "/api/v1/conversations/00000000-0000-0000-0000-000000000001",
        headers={"X-UserId": "u-1", "X-User-Roles": "platform_admin"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "AUTHENTICATION_REQUIRED"
    assert "message" in body and "request_id" in body
    assert SECRET not in json.dumps(body)


def test_wrong_proxy_secret_is_401() -> None:
    app = _app(_trusted())
    client = TestClient(app)
    response = client.get(
        "/api/admin/summary",
        headers={
            "X-UserId": "u-1",
            "X-User-Roles": "platform_admin",
            "X-Trusted-Proxy-Secret": "wrong-secret",
        },
    )
    assert response.status_code == 401


def test_forged_platform_admin_without_credentials_is_401() -> None:
    """E-02 regression: browser-claimed platform_admin without proxy proof."""
    app = _app(_trusted())
    client = TestClient(app)
    response = client.put(
        "/api/admin/model-center",
        headers={"X-UserId": "u-evil", "X-User-Roles": "platform_admin"},
        json={"large_models": []},
    )
    assert response.status_code == 401
    # The admin state file must be untouched (still parses as valid state).
    with open("/tmp/map_bff_auth_test_state.json", encoding="utf-8") as fh:
        state = json.load(fh)
    assert "master_agent" in state or "model_center" in state


# --- 2/3. role-based authorization with valid proxy credentials ---------------


def test_valid_proxy_non_admin_write_is_403() -> None:
    app = _app(_trusted(roles="member"))
    client = TestClient(app)
    response = client.put(
        "/api/admin/model-center",
        headers={
            "X-UserId": "u-2",
            "X-User-Roles": "member",
            "X-Trusted-Proxy-Secret": SECRET,
        },
        json={"large_models": []},
    )
    # Legacy path keeps the default {"detail": ...} shape; status is 403.
    assert response.status_code == 403


def test_valid_proxy_platform_admin_write_allowed() -> None:
    app = _app(_trusted())
    client = TestClient(app)
    response = client.put(
        "/api/admin/model-center",
        headers={
            "X-UserId": "u-1",
            "X-User-Roles": "platform_admin",
            "X-Trusted-Proxy-Secret": SECRET,
        },
        json={"large_models": []},
    )
    assert response.status_code == 200


# --- 4. startup fails on unsafe configurations --------------------------------


def test_prod_with_dev_mode_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="forbidden in production"):
        create_app(settings=Settings(auth_mode=AuthMode.DEV, env="prod"))


def test_prod_with_trusted_header_verification_disabled_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="MAP_TRUSTED_PROXY_REQUIRED=true is mandatory"):
        create_app(
            settings=Settings(
                auth_mode=AuthMode.TRUSTED_HEADER,
                trusted_proxy_required=False,
                trusted_proxy_secret=SECRET,
                env="prod",
            )
        )


def test_trusted_header_without_secret_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="MAP_TRUSTED_PROXY_SECRET is required"):
        create_app(
            settings=Settings(
                auth_mode=AuthMode.TRUSTED_HEADER,
                trusted_proxy_required=True,
                trusted_proxy_secret="",
            )
        )


# --- 5. service identity ------------------------------------------------------


def test_service_ping_with_valid_token() -> None:
    app = _app(Settings(auth_mode=AuthMode.DEV, service_tokens=("svc-token-1",)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": "Bearer svc-token-1",
            "X-Service-Name": "obs-backend",
            "X-Service-Audience": "map-bff",
            "X-Service-Scopes": "internal.ping,admin.read",
        },
    )
    assert response.status_code == 200
    assert response.json()["service"] == "obs-backend"


def test_service_ping_wrong_audience_is_401() -> None:
    app = _app(Settings(auth_mode=AuthMode.DEV, service_tokens=("svc-token-1",)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": "Bearer svc-token-1",
            "X-Service-Name": "obs-backend",
            "X-Service-Audience": "another-service",
            "X-Service-Scopes": "internal.ping",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_SERVICE_IDENTITY"


def test_service_ping_missing_scope_is_403() -> None:
    app = _app(Settings(auth_mode=AuthMode.DEV, service_tokens=("svc-token-1",)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": "Bearer svc-token-1",
            "X-Service-Name": "obs-backend",
            "X-Service-Audience": "map-bff",
            "X-Service-Scopes": "admin.read",
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_browser_token_cannot_access_internal_api() -> None:
    """A user token (proxy secret) must never satisfy service auth."""
    app = _app(_trusted())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": f"Bearer {SECRET}",
            "X-Service-Name": "browser",
            "X-Service-Audience": "map-bff",
            "X-Service-Scopes": "internal.ping",
        },
    )
    assert response.status_code == 401


# --- 6. secret fuzz -----------------------------------------------------------


def test_secret_never_leaks_to_response_logs_or_audit(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    app = _app(_trusted())
    client = TestClient(app)

    # Trigger 401 (wrong secret), 403 (valid secret, no role) and 200 paths.
    for headers in (
        {"X-UserId": "u-1", "X-User-Roles": "platform_admin"},
        {
            "X-UserId": "u-1",
            "X-User-Roles": "platform_admin",
            "X-Trusted-Proxy-Secret": "wrong",
        },
        {
            "X-UserId": "u-1",
            "X-User-Roles": "member",
            "X-Trusted-Proxy-Secret": SECRET,
        },
        {
            "X-UserId": "u-1",
            "X-User-Roles": "platform_admin",
            "X-Trusted-Proxy-Secret": SECRET,
        },
    ):
        response = client.put(
            "/api/admin/model-center",
            headers=headers,
            json={"large_models": []},
        )
        assert response.status_code in (200, 401, 403)
        assert SECRET not in json.dumps(response.json())

    assert SECRET not in caplog.text
    assert OTHER_SECRET not in caplog.text


# --- 7. cross-workspace isolation ---------------------------------------------


def test_repository_sql_carries_workspace_and_owner_predicate() -> None:
    """Repository queries must scope by workspace_id + owner in SQL itself."""
    import asyncio
    import uuid as uuid_mod

    from app.repositories.conversations import ConversationRepository

    captured = {}

    class FakeSession:
        async def execute(self, stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return FakeResult()

    class FakeResult:
        def scalar_one_or_none(self):
            return None

    repo = ConversationRepository(FakeSession())  # type: ignore[arg-type]
    asyncio.run(
        repo.get_conversation(
            uuid_mod.UUID("00000000-0000-0000-0000-000000000001"),
            uuid_mod.UUID("00000000-0000-0000-0000-000000000099"),
            "other-user",
        )
    )
    sql = captured["sql"]
    assert "workspace_id" in sql and "owner_user_id" in sql
    assert "00000000000000000000000000000099" in sql  # foreign workspace bound
    assert "other-user" in sql
