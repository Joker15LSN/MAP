"""R1-AUDIT-01 / R2-P1-02 acceptance: admin writes are audited with the
trusted actor into the hash-chained ``config_audit_events``.

Actor comes from the RequestPrincipal, never from the request body; every
successful admin write produces one applied admin audit event plus one
applied runtime_snapshot audit event (Step 7 PR-J3); rejected attempts
produce a rejected admin event; the legacy ``audit_logs`` table receives
no new product writes (the /api/admin/audit-logs facade maps the new
events instead).
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_audit_test_state.json")

import pytest
import pytest_asyncio
from conftest import seed_pg_admin_state
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.identity import AuthMode
from app.db.models import AuditLog, ConfigAuditEvent
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
    await seed_pg_admin_state(session)
    return app, session


async def _events(session) -> list:
    rows = (
        await session.execute(
            text(
                "SELECT actor_user_id, action, resource_type, resource_id, status, "
                "request_id FROM map_control.config_audit_events ORDER BY ordinal"
            )
        )
    ).all()
    return list(rows)


async def test_admin_write_produces_exactly_one_admin_and_one_snapshot_event(
    app_and_session,
) -> None:
    app, session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/admin/model-center",
            json={"large_models": []},
            headers={"X-Request-ID": "audit-req-1"},
        )
        assert response.status_code == 200

    rows = await _events(session)
    assert len(rows) == 2
    admin_row, snapshot_row = rows
    assert admin_row.actor_user_id == "local-admin"  # from principal, dev mode
    assert admin_row.action == "update"
    assert admin_row.resource_type == "admin_config"
    assert admin_row.resource_id == "model_center"
    assert admin_row.status == "applied"
    assert admin_row.request_id == "audit-req-1"
    assert snapshot_row.actor_user_id == "local-admin"
    assert snapshot_row.action == "activate"
    assert snapshot_row.resource_type == "runtime_snapshot"
    assert snapshot_row.status == "applied"
    assert snapshot_row.request_id == "audit-req-1"


async def test_client_operator_field_cannot_change_actor(app_and_session) -> None:
    app, session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The legacy release-history endpoint accepts operator in the query;
        # the audit actor must still be the trusted principal.
        response = await client.post(
            "/api/admin/release-history?note=test&operator=attacker",
            headers={"X-Request-ID": "audit-req-2"},
        )
        assert response.status_code == 200

    rows = await _events(session)
    assert len(rows) == 2  # 1 admin audit + 1 runtime_snapshot audit
    for row in rows:
        assert row.actor_user_id == "local-admin"
        assert row.actor_user_id != "attacker"


async def test_every_successful_write_audited_once(app_and_session) -> None:
    """Even an identical (content-wise no-op) write is an attempted write:
    one admin applied event + one snapshot applied event per successful
    call, never zero, never three."""
    app, session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put("/api/admin/model-center", json={"large_models": []})
        assert first.status_code == 200
        second = await client.put("/api/admin/model-center", json={"large_models": []})
        assert second.status_code == 200

    rows = await _events(session)
    assert len(rows) == 4
    assert {row.status for row in rows} == {"applied"}
    assert sum(1 for row in rows if row.resource_type == "runtime_snapshot") == 2
    assert sum(1 for row in rows if row.resource_type != "runtime_snapshot") == 2


async def test_rejected_write_audited_and_legacy_table_gets_no_new_writes(
    app_and_session,
) -> None:
    """A business rejection (409 duplicate agent) leaves one rejected admin
    event; the successful create left one admin + one snapshot event. The
    legacy audit_logs table stays empty."""
    app, session = app_and_session
    transport = ASGITransport(app=app)
    agent = {
        "agent_code": "audit-agent",
        "display_name": "审计代理",
        "scene_name": "audit",
        "owner_team": "map",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/admin/business-agents", json=agent)
        assert created.status_code == 200
        duplicate = await client.post("/api/admin/business-agents", json=agent)
        assert duplicate.status_code == 409

    rows = await _events(session)
    assert len(rows) == 3
    statuses = [row.status for row in rows]
    assert statuses == ["applied", "applied", "rejected"]
    assert rows[1].resource_type == "runtime_snapshot"
    assert rows[2].resource_type == "business_agent"
    assert rows[2].resource_id == "audit-agent"

    # R2-P1-02: no new product write may land in the legacy table.
    legacy = (await session.execute(select(AuditLog))).scalars().all()
    assert legacy == []


async def test_audit_logs_facade_maps_new_events(app_and_session) -> None:
    app, _session = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put("/api/admin/model-center", json={"large_models": []})
        await client.put("/api/admin/basic-settings", json=[])

        all_rows = await client.get("/api/admin/audit-logs")
        assert all_rows.status_code == 200
        # 2 PUTs -> 2 admin audit events + 2 runtime_snapshot audit events.
        assert all_rows.json()["total"] == 4
        for item in all_rows.json()["items"]:
            assert item["status"] == "applied"

        filtered = await client.get(
            "/api/admin/audit-logs", params={"resource_type": "admin_config"}
        )
        assert filtered.json()["total"] == 2
        for item in filtered.json()["items"]:
            assert item["resource_type"] == "admin_config"

        by_actor = await client.get("/api/admin/audit-logs", params={"actor": "local-admin"})
        assert by_actor.json()["total"] == 4


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

    rows = (await session.execute(select(ConfigAuditEvent))).scalars().all()
    assert rows == []
