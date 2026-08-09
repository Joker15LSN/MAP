"""R1-AUDIT-01 acceptance: admin writes are audited with the trusted actor.

Actor comes from the RequestPrincipal, never from the request body; a
state-changing admin write produces exactly one audit row; a no-op write
produces none; the query API filters by actor/action/resource.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_audit_test_state.json")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.identity import AuthMode
from app.db.models import AuditLog
from app.db.session import get_db_session
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.asyncio

WORKSPACE = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


@pytest_asyncio.fixture
async def app_and_session(_engine, session):
    import uuid as _uuid

    state_file = f"/tmp/map_bff_audit_state_{_uuid.uuid4().hex[:8]}.json"
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=state_file,
            default_workspace_id=WORKSPACE,
        ),
    )

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override
    return app, session


async def test_admin_write_produces_audit_row(app_and_session) -> None:
    app, session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/admin/model-center",
            json={"large_models": []},
            headers={"X-Request-ID": "audit-req-1"},
        )
        assert response.status_code == 200

    rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == "local-admin"  # from principal, dev mode
    assert row.action == "config.update"
    assert row.resource_type == "model-center"
    assert row.request_id == "audit-req-1"
    assert row.before_json is not None and row.after_json is not None


async def test_client_operator_field_cannot_change_actor(app_and_session) -> None:
    app, session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The legacy release-history endpoint accepts operator in the body;
        # the audit actor must still be the trusted principal.
        response = await client.post(
            "/api/admin/release-history?note=test&operator=attacker",
            headers={"X-Request-ID": "audit-req-2"},
        )
        assert response.status_code == 200

    rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_user_id == "local-admin"
    assert rows[0].actor_user_id != "attacker"


async def test_noop_write_produces_no_audit_row(app_and_session) -> None:
    app, session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put("/api/admin/model-center", json={"large_models": []})
        assert first.status_code == 200
        # Identical payload: state unchanged -> no second audit row.
        second = await client.put("/api/admin/model-center", json={"large_models": []})
        assert second.status_code == 200

    rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1


async def test_audit_query_api_filters(app_and_session) -> None:
    app, _session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put("/api/admin/model-center", json={"large_models": []})
        await client.put("/api/admin/basic-settings", json=[])

        all_rows = await client.get("/api/admin/audit-logs")
        assert all_rows.status_code == 200
        assert all_rows.json()["total"] == 2

        filtered = await client.get(
            "/api/admin/audit-logs", params={"resource_type": "basic-settings"}
        )
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["resource_type"] == "basic-settings"

        by_actor = await client.get("/api/admin/audit-logs", params={"actor": "local-admin"})
        assert by_actor.json()["total"] == 2


async def test_non_admin_write_is_403_and_not_audited(app_and_session) -> None:
    app, session = app_and_session
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_audit_state.json",
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]
    transport = ASGITransport(app=other_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/admin/model-center",
            json={"large_models": []},
            headers={
                "X-UserId": "u-noadmin",
                "X-User-Roles": "member",
                "X-Trusted-Proxy-Secret": "s3cret",
            },
        )
        assert response.status_code == 403

    rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 0
