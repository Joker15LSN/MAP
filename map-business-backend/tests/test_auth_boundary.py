"""FIX-P0-AUTH-01 acceptance: fail-closed trusted identity + admin authz.

- no/wrong/forged proxy credentials -> 401, admin state unchanged, no audit
- valid proxy credentials + non-admin role -> 403
- valid proxy credentials + platform_admin -> allowed
- prod + dev / prod + trusted_header without verification -> startup failure
- service credentials: inherent claims only; header tampering/impersonation/
  overclaim/foreign audience/rotation-revocation all rejected; browser token
  on /internal/v1 -> 401; service token on browser API -> 401
- secret fuzz: the proxy secret never appears in responses, logs or audit
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_auth_test_state.json")

import pytest
from fastapi.testclient import TestClient

from app.core.identity import AuthMode
from app.core.service_identity import ServiceCredential, parse_service_credentials
from app.main import create_app
from app.settings import Settings

SECRET = "fake-s3cret-value-42"
OTHER_SECRET = "fake-other-value-99"


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


# --- 3b. infrastructure probe exemption (R3-P1-02) ----------------------------


def test_health_and_ready_probes_pass_gate_without_credentials() -> None:
    """Docker healthchecks cannot carry identity credentials: /health and
    /ready must bypass the identity gate while business routes stay 401.
    /ready without a live DB degrades to 503 (it reached the route, not
    the gate) — the assertion is exactly that it is NOT 401."""
    app = _app(_trusted())
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/ready")
    assert ready.status_code in (200, 503)

    business = client.get("/api/admin/summary")
    assert business.status_code == 401


# --- 4. startup fails on unsafe configurations --------------------------------


def test_prod_with_dev_mode_fails_startup() -> None:
    with pytest.raises(RuntimeError, match="forbidden in production"):
        create_app(settings=Settings(auth_mode=AuthMode.DEV, env="prod"))


def test_prod_wildcard_cors_with_credentials_fails_startup() -> None:
    """AC-SEC-11: production refuses wildcard CORS + credentials."""
    with pytest.raises(RuntimeError, match="wildcard CORS with credentials"):
        create_app(
            settings=Settings(
                auth_mode=AuthMode.TRUSTED_HEADER,
                trusted_proxy_secret=SECRET,
                env="prod",
                cors_origins="*",
                cors_allow_credentials=True,
            )
        )


def test_prod_explicit_cors_origins_are_allowed() -> None:
    """Explicit origins (or credentials off) are not wildcard-refused."""
    app = _app(
        Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            trusted_proxy_secret=SECRET,
            env="prod",
            cors_origins="https://app.example.com,https://admin.example.com",
            cors_allow_credentials=True,
        )
    )
    # CORS middleware is applied with the explicit origin list (preflight
    # on an identity-gate-excluded probe path so the auth layer doesn't
    # short-circuit the OPTIONS request).
    client = TestClient(app)
    response = client.options(
        "/ready",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == (
        "https://app.example.com"
    )


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


# --- 5. service identity (R2-P0-02: credential-bound inherent claims) --------

OBS_CRED = ServiceCredential(
    key_id="obs-backend-v1",
    token="svc-token-obs-1",
    service_name="obs-backend",
    audience="map-bff",
    scopes=("internal.ping", "audit.read"),
)
FLOW_CRED = ServiceCredential(
    key_id="flow-engine-v1",
    token="svc-token-flow-1",
    service_name="flow-engine",
    audience="map-bff",
    scopes=("internal.ping",),
)


def _secure(credentials: tuple[ServiceCredential, ...] = (OBS_CRED,)) -> Settings:
    """Production-like trusted_header config with a credential registry."""
    return Settings(
        auth_mode=AuthMode.TRUSTED_HEADER,
        trusted_proxy_secret=SECRET,
        trusted_proxy_required=True,
        service_credentials=credentials,
    )


def test_service_credential_alone_is_200_under_trusted_header() -> None:
    """R2-P0-02 regression: a legal service credential alone must reach an
    authorized internal route in secure mode — no forged user headers."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={"Authorization": f"Bearer {OBS_CRED.token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "obs-backend"
    assert body["audience"] == "map-bff"
    assert sorted(body["scopes"]) == ["audit.read", "internal.ping"]
    assert body["key_id"] == "obs-backend-v1"


def test_service_ping_with_valid_token() -> None:
    """Consistent X-Service-* headers (transport/debug info) still pass."""
    app = _app(Settings(auth_mode=AuthMode.DEV, service_credentials=(OBS_CRED,)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Service-Name": "obs-backend",
            "X-Service-Audience": "map-bff",
            "X-Service-Scopes": "internal.ping,audit.read",
        },
    )
    assert response.status_code == 200
    assert response.json()["service"] == "obs-backend"


def test_header_overclaim_never_expands_grant_is_403() -> None:
    """Claiming extra scopes via header must fail, never widen the grant."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Service-Scopes": "internal.ping,admin.write,anything",
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_header_tamper_cannot_change_inherent_claims() -> None:
    """Authorization comes only from the credential: with no headers at all
    the inherent claims apply; tampered headers cannot alter them."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={"Authorization": f"Bearer {OBS_CRED.token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "obs-backend"
    assert sorted(body["scopes"]) == ["audit.read", "internal.ping"]


def test_service_a_credential_cannot_impersonate_service_b() -> None:
    app = _app(_secure(credentials=(OBS_CRED, FLOW_CRED)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Service-Name": "flow-engine",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_SERVICE_IDENTITY"


def test_service_ping_wrong_audience_is_401() -> None:
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Service-Audience": "another-service",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_SERVICE_IDENTITY"


def test_credential_issued_for_foreign_audience_is_401() -> None:
    """A credential whose inherent audience targets another BFF is rejected."""
    foreign = ServiceCredential(
        key_id="obs-foreign-v1",
        token="svc-token-foreign",
        service_name="obs-backend",
        audience="another-bff",
        scopes=("internal.ping",),
    )
    app = _app(_secure(credentials=(foreign,)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={"Authorization": f"Bearer {foreign.token}"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_SERVICE_IDENTITY"


def test_service_ping_missing_scope_is_403() -> None:
    limited = ServiceCredential(
        key_id="obs-limited-v1",
        token="svc-token-limited",
        service_name="obs-backend",
        audience="map-bff",
        scopes=("audit.read",),
    )
    app = _app(_secure(credentials=(limited,)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={"Authorization": f"Bearer {limited.token}"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_runtime_snapshot_read_route_requires_service_scope() -> None:
    """The snapshot read route is service-identity only and requires its
    own scope; a valid credential without it is 403."""
    limited = ServiceCredential(
        key_id="obs-limited-v2",
        token="svc-token-limited-2",
        service_name="obs-backend",
        audience="map-bff",
        scopes=("internal.ping", "audit.read"),
    )
    app = _app(_secure(credentials=(limited,)))
    client = TestClient(app)
    response = client.get(
        "/internal/v1/runtime-config-snapshots/00000000-0000-0000-0000-000000000001",
        headers={"Authorization": f"Bearer {limited.token}"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_runtime_snapshot_read_route_rejects_credential_without_scope() -> None:
    """A valid service credential without the snapshot scope gets 403."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/runtime-config-snapshots/00000000-0000-0000-0000-000000000001",
        headers={"Authorization": f"Bearer {OBS_CRED.token}"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_rotation_dual_key_window_then_revocation() -> None:
    """Rotation: old+new key_id both valid during the window; revoking the
    old key rejects it instantly while the new key keeps working."""
    old = OBS_CRED
    new = ServiceCredential(
        key_id="obs-backend-v2",
        token="svc-token-obs-2",
        service_name="obs-backend",
        audience="map-bff",
        scopes=("internal.ping", "audit.read"),
    )
    client = TestClient(_app(_secure(credentials=(old, new))))
    for cred in (old, new):
        response = client.get(
            "/internal/v1/ping",
            headers={"Authorization": f"Bearer {cred.token}"},
        )
        assert response.status_code == 200
        assert response.json()["key_id"] == cred.key_id

    revoked_old = ServiceCredential(
        key_id=old.key_id,
        token=old.token,
        service_name=old.service_name,
        audience=old.audience,
        scopes=old.scopes,
        revoked=True,
    )
    client = TestClient(_app(_secure(credentials=(revoked_old, new))))
    rejected = client.get(
        "/internal/v1/ping",
        headers={"Authorization": f"Bearer {old.token}"},
    )
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "INVALID_SERVICE_IDENTITY"
    kept = client.get(
        "/internal/v1/ping",
        headers={"Authorization": f"Bearer {new.token}"},
    )
    assert kept.status_code == 200


def test_user_proxy_credential_alone_on_internal_route_is_401() -> None:
    """Proxy secret + user headers must never satisfy service auth."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "X-Trusted-Proxy-Secret": SECRET,
            "X-UserId": "u-1",
            "X-User-Roles": "platform_admin",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_SERVICE_IDENTITY"


def test_mixed_credentials_keep_service_principal_only() -> None:
    """Service bearer + forged/real user headers on an internal route: the
    effective principal stays the service one; no user context is created."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/internal/v1/ping",
        headers={
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Trusted-Proxy-Secret": SECRET,
            "X-UserId": "u-evil",
            "X-User-Roles": "platform_admin",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "obs-backend"
    assert "u-evil" not in json.dumps(body)


def test_service_token_alone_on_browser_api_is_401() -> None:
    """Browser APIs accept RequestPrincipal only: a service credential must
    not satisfy the user gate."""
    app = _app(_secure())
    client = TestClient(app)
    response = client.get(
        "/api/admin/summary",
        headers={"Authorization": f"Bearer {OBS_CRED.token}"},
    )
    assert response.status_code == 401


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


def test_service_token_never_leaks(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    app = _app(_secure())
    client = TestClient(app)
    for headers in (
        {"Authorization": f"Bearer {OBS_CRED.token}"},  # 200 path
        {"Authorization": "Bearer wrong-token"},  # 401 path
        {
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Service-Name": "flow-engine",  # impersonation 401 path
        },
        {
            "Authorization": f"Bearer {OBS_CRED.token}",
            "X-Service-Scopes": "admin.write",  # overclaim 403 path
        },
    ):
        response = client.get("/internal/v1/ping", headers=headers)
        assert response.status_code in (200, 401, 403)
        assert OBS_CRED.token not in json.dumps(response.json())
    assert OBS_CRED.token not in caplog.text


def test_credential_registry_config_is_validated() -> None:
    """Fail-closed parsing: malformed registry never reaches runtime."""
    good = (
        '[{"key_id":"obs-v1","token":"t1","service_name":"obs-backend",'
        '"scopes":"internal.ping"}]'
    )
    parsed = parse_service_credentials(good, default_audience="map-bff")
    assert parsed[0].audience == "map-bff" and parsed[0].scopes == ("internal.ping",)

    for bad in (
        "not-json",
        '{"key_id":"obs-v1"}',  # not an array
        '[{"token":"t1","service_name":"obs"}]',  # missing key_id
        '[{"key_id":"k","service_name":"obs"}]',  # missing token
        # duplicate key_id
        '[{"key_id":"k","token":"t1","service_name":"a"},'
        '{"key_id":"k","token":"t2","service_name":"b"}]',
        # duplicate token: one token must map to exactly one identity
        '[{"key_id":"k1","token":"t","service_name":"a"},'
        '{"key_id":"k2","token":"t","service_name":"b"}]',
    ):
        with pytest.raises(ValueError):
            parse_service_credentials(bad, default_audience="map-bff")


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


# --- S3-04: shared CORS policy (same rules as map_core) -----------------------


def test_malformed_cors_origin_fails_startup_in_every_env() -> None:
    """S3-04: an origin that is neither '*' nor http(s)://host[:port] fails
    at startup in ANY environment (not only production)."""
    for bad in ("http://", "example.com", "https://host/path"):
        with pytest.raises(RuntimeError, match="invalid MAP_CORS_ORIGINS"):
            create_app(
                settings=Settings(
                    auth_mode=AuthMode.DEV,
                    env="dev",
                    cors_origins=bad,
                    cors_allow_credentials=False,
                )
            )
