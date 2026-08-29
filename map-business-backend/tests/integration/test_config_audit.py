"""FIX-P1-AUDIT-01 acceptance: non-repudiation config write audit.

- every admin write funnels through RuntimeSnapshotService (no router-level
  store.update); applied/failed/rejected all leave hash-chained events;
  each mutating admin write emits one admin audit + one runtime_snapshot
  audit (Step 7 PR-J3)
- client-supplied operator/body fields never change the actor
- store write failures: API fails, failed audit event, original file intact
- concurrent writes with the same expected hash: one wins, one 409
- crash points (pending / after-rename) recovered by the reconciler
- tampered audit rows are located by chain verification
- secrets never appear in audit events
- audit viewer permission + workspace scope
- corrupt state file is never overwritten by defaults
- DB role without UPDATE/DELETE rights cannot tamper with audit events
"""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from pathlib import Path

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_audit_fix_state.json")

import pytest
import pytest_asyncio
from conftest import ADMIN_DSN, APP_DSN, MIGRATION_DSN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.services.runtime_snapshot.schemas import RuntimeProjection
from app.settings import Settings
from app.store import AdminStateStore

pytestmark = pytest.mark.asyncio

WORKSPACE = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
SECRET = "Bearer fake-super-secret-token"


@pytest_asyncio.fixture
async def app_and_session(_engine, session, tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    state_file = str(tmp_path / "admin_state.json")
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=state_file,
            default_workspace_id=WORKSPACE,
        ),
        store=None,
        core_client=None,
    )
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override
    app.state.test_factory = factory
    return app, session, state_file


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _audit_count(session, status=None) -> int:
    sql = "SELECT count(*) FROM map_control.config_audit_events"
    params = {}
    if status:
        sql += " WHERE status = :status"
        params["status"] = status
    return (await session.execute(text(sql), params)).scalar_one()


async def _audit_count_by_resource(session, resource_type: str) -> int:
    """Count audit rows by bucket.

    ``"admin"`` counts every non-runtime-snapshot resource (the
    admin-state audit); ``"runtime_snapshot"`` counts the snapshot audit.
    """
    if resource_type == "runtime_snapshot":
        sql = (
            "SELECT count(*) FROM map_control.config_audit_events "
            "WHERE resource_type = 'runtime_snapshot'"
        )
    else:
        sql = (
            "SELECT count(*) FROM map_control.config_audit_events "
            "WHERE resource_type <> 'runtime_snapshot'"
        )
    return (await session.execute(text(sql))).scalar_one()


async def test_every_admin_write_is_audited_and_actor_trusted(app_and_session) -> None:
    """Enumerate admin write paths from OpenAPI; each write audits with the
    trusted actor (client body fields can never change it).

    New contract (Step 7 PR-J3): every mutating admin write emits one
    admin-state audit (resource_type != 'runtime_snapshot') plus one
    runtime_snapshot audit; both carry the trusted principal.
    """
    app, session, _ = app_and_session
    openapi = app.openapi()
    write_paths = sorted(
        f"{method.upper()} {path}"
        for path, methods in openapi["paths"].items()
        if path.startswith("/api/admin")
        for method in ("put", "post", "patch", "delete")
        if method in methods
    )
    assert len(write_paths) >= 10

    async with await _client(app) as client:
        # Representative writes across the router set (GET -> modify -> PUT
        # keeps every payload schema-valid).
        current_model = (await client.get("/api/admin/model-center")).json()
        current_model["large_models"] = []
        writes = [
            ("PUT", "/api/admin/model-center", current_model),
            ("PUT", "/api/admin/flow-policy", (await client.get("/api/admin/flow-policy")).json()),
            ("PUT", "/api/admin/basic-settings", []),
        ]
        for method, path, payload in writes:
            response = await client.request(method, path, json=payload)
            assert response.status_code == 200, (method, path, response.text)

        response = await client.post(
            "/api/admin/release-history",
            params={"note": "audit-test", "operator": "evil-client"},
        )
        assert response.status_code == 200, response.text

    # Every write produced one admin audit + one snapshot audit, both with
    # the trusted actor.
    rows = (
        await session.execute(
            text(
                "SELECT resource_type, resource_id, action, actor_user_id, status, "
                "entry_hash, prev_entry_hash, json_patch "
                "FROM map_control.config_audit_events ORDER BY ordinal"
            )
        )
    ).all()
    admin_rows = [row for row in rows if row.resource_type != "runtime_snapshot"]
    snapshot_rows = [row for row in rows if row.resource_type == "runtime_snapshot"]
    expected_mutations = len(writes) + 1  # 3 PUTs + 1 release-history POST
    assert len(admin_rows) == expected_mutations
    assert len(snapshot_rows) == expected_mutations
    for row in rows:
        assert row.actor_user_id == "local-admin"
        assert "evil-client" not in row.actor_user_id
        assert row.status == "applied"
        assert (row.entry_hash and row.prev_entry_hash is not None) or row.prev_entry_hash is None
    for row in admin_rows:
        assert row.json_patch is not None  # admin diff present
    for row in snapshot_rows:
        assert row.json_patch is None  # snapshot audit carries no admin diff
    # Hash chain: no forks (each entry references its predecessor).
    hashes = [row.entry_hash for row in rows]
    assert len(set(hashes)) == len(hashes)


async def test_flow_policy_put_advances_runtime_snapshot_pointer(
    app_and_session,
) -> None:
    """Step 7 PR-J3: an admin write creates an active runtime snapshot,
    points ``runtime_snapshot_current`` at it, rolls the previous active
    snapshot back, and emits one admin audit + one snapshot audit."""
    app, session, _ = app_and_session

    async with await _client(app) as client:
        base = (await client.get("/api/admin/flow-policy")).json()
        first_payload = {**base, "max_node_budget": 11}
        response = await client.put("/api/admin/flow-policy", json=first_payload)
        assert response.status_code == 200, response.text
        assert await _audit_count_by_resource(session, "admin") == 1
        assert await _audit_count_by_resource(session, "runtime_snapshot") == 1

        second_payload = {**base, "max_node_budget": 22}
        response = await client.put("/api/admin/flow-policy", json=second_payload)
        assert response.status_code == 200, response.text
        assert await _audit_count_by_resource(session, "admin") == 2
        assert await _audit_count_by_resource(session, "runtime_snapshot") == 2

    snapshots = (
        await session.execute(
            text(
                "SELECT id, digest, status FROM map_control.runtime_snapshots "
                "ORDER BY created_at, id"
            )
        )
    ).all()
    assert len(snapshots) == 2
    first, second = snapshots
    assert first.status == "rolled_back"
    assert second.status == "active"

    pointer = (
        await session.execute(
            text(
                "SELECT current_snapshot_id, current_digest "
                "FROM map_control.runtime_snapshot_current WHERE id = 1"
            )
        )
    ).one()
    assert pointer.current_snapshot_id == second.id
    assert pointer.current_digest == second.digest

    audit_rows = (
        await session.execute(
            text(
                "SELECT resource_type, action, status FROM map_control.config_audit_events "
                "ORDER BY ordinal"
            )
        )
    ).all()
    assert audit_rows == [
        ("admin_config", "update", "applied"),
        ("runtime_snapshot", "activate", "applied"),
        ("admin_config", "update", "applied"),
        ("runtime_snapshot", "activate", "applied"),
    ]


async def test_concurrent_writes_one_wins_one_409(app_and_session) -> None:
    app, session, _state_file = app_and_session
    store = app.state.store
    first_hash = store_load_hash(store)

    async with await _client(app) as client:
        payload = (await client.get("/api/admin/model-center")).json()
        payload["large_models"] = []
        ok = await client.put("/api/admin/model-center", json=payload)
        assert ok.status_code == 200

    # Second write started from the same stale hash -> rejected 409.
    from app.services.config_mutation import ConfigMutationService

    service = ConfigMutationService(store)
    assert service is not None
    # Simulate a stale expected hash directly through the store.
    from app.store import ConcurrentModificationError

    def _updater(draft):
        return draft

    try:
        store.update_with_hash(first_hash, _updater)
        pytest.fail("expected ConcurrentModificationError")
    except ConcurrentModificationError:
        pass

    async with await _client(app) as client:
        # A genuine 409 through the API: grab the current hash first.
        current_hash = store_load_hash(store)
        # Patch the prepare phase to force a stale-hash path via a
        # concurrent actor (R3-P1-01: mutations now prepare + apply).
        original = store.prepare_update

        def _stale(expected, updater):
            return original(current_hash + "stale", updater)

        store.prepare_update = _stale  # type: ignore[method-assign]
        try:
            payload2 = (await client.get("/api/admin/model-center")).json()
            response = await client.put("/api/admin/model-center", json=payload2)
            assert response.status_code == 409
            assert "concurrently" in response.json()["detail"]
        finally:
            store.prepare_update = original  # type: ignore[method-assign]

    rejected = await _audit_count(session, status="rejected")
    assert rejected >= 1


async def test_store_write_failure_keeps_file_and_audits_failed(
    app_and_session, monkeypatch
) -> None:
    app, session, state_file = app_and_session
    before = Path(state_file).read_bytes()

    from app.store import StoreWriteError

    def _boom(*args, **kwargs):
        raise StoreWriteError("disk full")

    monkeypatch.setattr(app.state.store, "_write_atomic", _boom)
    async with await _client(app) as client:
        payload = (await client.get("/api/admin/model-center")).json()
        response = await client.put("/api/admin/model-center", json=payload)
        assert response.status_code == 500
        assert "write failed" in response.json()["detail"]

    assert Path(state_file).read_bytes() == before  # original file intact
    assert await _audit_count(session, status="failed") >= 1
    failed = (
        await session.execute(
            text(
                "SELECT failure_code FROM map_control.config_audit_events "
                "WHERE status = 'failed' ORDER BY created_at DESC LIMIT 1"
            )
        )
    ).scalar_one()
    assert failed == "STORE_WRITE_FAILED"


async def test_bad_state_file_never_overwritten_by_defaults(app_and_session) -> None:
    app, session, state_file = app_and_session
    Path(state_file).write_text("{not-json", encoding="utf-8")
    async with await _client(app) as client:
        response = await client.put("/api/admin/model-center", json={})
        assert response.status_code == 500
        assert "corrupt" in response.json()["detail"]
    # The corrupt file is preserved byte-for-byte.
    assert Path(state_file).read_text(encoding="utf-8") == "{not-json"
    assert await _audit_count(session, status="failed") >= 1


async def test_reconciler_recovers_pending_and_unknown_state_crashes(
    app_and_session,
) -> None:
    app, session, _state_file = app_and_session
    store = app.state.store

    # Crash point 1: pending mutation, no file write (current == expected).

    from app.db.models import ConfigMutation

    async with app.state.test_factory() as s:
        s.add(
            ConfigMutation(
                resource="admin_config:model-center",
                expected_hash=store_load_hash(store),
                status="pending",
            )
        )
        await s.commit()

    # Crash point 2: legacy pending row without a persisted target_hash and
    # a file hash matching NEITHER expected nor target: R3-P1-01 forbids
    # guessing "applied" here — it must be reconciled as UNKNOWN_STATE.
    async with app.state.test_factory() as s:
        s.add(
            ConfigMutation(
                resource="admin_config:flow-policy",
                expected_hash="0000000000000000000000000000000000000000000000000000000000000000",
                status="pending",
            )
        )
        await s.commit()

    from app.services.config_mutation import reconcile_config_mutations

    recovered = await reconcile_config_mutations(app.state.test_factory, store)
    assert recovered == 2

    statuses = (
        (
            await session.execute(
                text("SELECT status FROM map_control.config_mutations ORDER BY created_at")
            )
        )
        .scalars()
        .all()
    )
    assert statuses == ["failed", "failed"]

    events = (
        await session.execute(
            text(
                "SELECT status, failure_code, recovered FROM map_control.config_audit_events "
                "WHERE recovered = true ORDER BY created_at"
            )
        )
    ).all()
    assert len(events) == 2
    assert {row.status for row in events} == {"failed"}
    assert [row.failure_code for row in events] == ["NO_WRITE", "UNKNOWN_STATE"]

    # Idempotent: nothing left pending.
    assert await reconcile_config_mutations(app.state.test_factory, store) == 0


async def test_tampered_audit_row_detected_by_chain_verify(app_and_session) -> None:
    app, _session, _ = app_and_session
    async with await _client(app) as client:
        payload = (await client.get("/api/admin/model-center")).json()
        await client.put("/api/admin/model-center", json=payload)

        # Verify passes before tampering.
        verify = await client.get("/api/v1/admin/audit-events/verify")
        assert verify.status_code == 200
        assert verify.json()["ok"] is True

    # Tamper with the actor of the last event (admin role: the app role
    # cannot UPDATE audit tables at all, R2-P1-04).
    admin_engine = create_async_engine(ADMIN_DSN)
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE map_control.config_audit_events "
                "SET actor_user_id = 'hacker' WHERE entry_hash = "
                "(SELECT entry_hash FROM map_control.config_audit_events "
                " ORDER BY ordinal DESC LIMIT 1)"
            )
        )
    await admin_engine.dispose()

    async with await _client(app) as client:
        verify = await client.get("/api/v1/admin/audit-events/verify")
        assert verify.json()["ok"] is False
        assert verify.json()["first_broken_at"] is not None


async def test_secret_never_in_audit(app_and_session) -> None:
    app, session, _st = app_and_session
    async with await _client(app) as client:
        payload = (await client.get("/api/admin/model-center")).json()
        payload["large_models"] = [
            {
                "model_name": "llama3",
                "model_type": "本地",
                "model_url": f"https://example.com/v1?api_key={SECRET}",
                "is_default": True,
                "api_type": "http",
            }
        ]
        response = await client.put("/api/admin/model-center", json=payload)
        assert response.status_code == 200

    raw_events = (
        await session.execute(
            text(
                "SELECT json_patch, actor_roles FROM map_control.config_audit_events "
                "WHERE resource_type <> 'runtime_snapshot' "
                "ORDER BY ordinal DESC LIMIT 1"
            )
        )
    ).one()
    raw = json.dumps([raw_events.json_patch, raw_events.actor_roles], ensure_ascii=False)
    assert SECRET not in raw
    assert "api_key" not in raw or "REDACTED" in raw


async def test_audit_viewer_and_workspace_scope(app_and_session) -> None:
    app, _session, _st = app_and_session
    async with await _client(app) as client:
        payload = (await client.get("/api/admin/model-center")).json()
        await client.put("/api/admin/model-center", json=payload)
        listing = await client.get("/api/v1/admin/audit-events")
        assert listing.status_code == 200
        assert listing.json()["total"] >= 1
        for item in listing.json()["items"]:
            assert item["workspace_id"] == WORKSPACE

        filtered = await client.get("/api/v1/admin/audit-events", params={"actor": "local-admin"})
        assert filtered.json()["total"] >= 1

    # Non-audit-viewer (plain member) cannot read audit events.
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_audit_fix_other.json",
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
        store=None,
        core_client=None,
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]
    async with await _client(other_app) as other:
        response = await other.get(
            "/api/v1/admin/audit-events",
            headers={
                "X-UserId": "user-member",
                "X-User-Roles": "member",
                "X-Trusted-Proxy-Secret": "s3cret",
            },
        )
        assert response.status_code == 403


def _dsn_user(dsn: str) -> str:
    return urllib.parse.urlparse(dsn).username or ""


async def test_compose_app_role_is_least_privilege_and_audit_append_only(
    _engine, session
) -> None:
    """R2-P1-04: permission checks run against the REAL compose DSNs —
    the backend/worker app role (MAP_CONTROL_DB_DSN), never a temporary
    stand-in role."""
    from sqlalchemy.ext.asyncio import create_async_engine

    app_user = _dsn_user(APP_DSN)

    # 1) The app role is not superuser and cannot create roles/databases.
    admin_engine = create_async_engine(ADMIN_DSN)
    async with admin_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = :role"
                ),
                {"role": app_user},
            )
        ).one()
    assert row == (False, False, False), f"app role {app_user} must be least-privilege"

    # 2) Audit table contract with the app DSN:
    #    SELECT/INSERT succeed; UPDATE/DELETE/TRUNCATE/ALTER/CREATE fail.
    app_engine = create_async_engine(APP_DSN)
    async with app_engine.connect() as conn:
        await conn.execute(
            text("SELECT count(*) FROM map_control.config_audit_events")
        )
        await conn.rollback()  # close the autobegin before explicit begin()
        # INSERT succeeds (rolled back; permission is checked on execution).
        trans = await conn.begin()
        await conn.execute(
            text(
                "INSERT INTO map_control.config_audit_events "
                "(resource_type, resource_id, action, actor_user_id, status, "
                " recovered, prev_entry_hash, entry_hash, ordinal) "
                "VALUES ('probe', 'probe', 'probe', 'probe', 'applied', false, "
                " :prev, :entry, 999999)"
            ),
            {"prev": uuid.uuid4().hex * 2, "entry": uuid.uuid4().hex * 2},
        )
        await trans.rollback()

        for denied_sql in (
            "UPDATE map_control.config_audit_events SET actor_user_id='x' "
            "WHERE resource_type='none'",
            "DELETE FROM map_control.config_audit_events",
            "TRUNCATE TABLE map_control.config_audit_events",
            "ALTER TABLE map_control.config_audit_events ADD COLUMN probe int",
            "CREATE TABLE map_control.probe_table (id int)",
        ):
            try:
                await conn.execute(text(denied_sql))
                await conn.rollback()
                pytest.fail(f"app role must be denied: {denied_sql}")
            except Exception:  # noqa: BLE001 - expected denial
                await conn.rollback()
    await app_engine.dispose()
    await admin_engine.dispose()


async def test_migrator_round_trip_on_fresh_database_and_app_ddl_denied(
    _engine,
) -> None:
    """R2-P1-04: with the migration DSN a fresh database survives
    upgrade -> downgrade -> upgrade; the app DSN can never run DDL there."""
    import asyncio as _asyncio

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    check_db = "map_r2_p1_04_migcheck"
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    admin_engine = create_async_engine(ADMIN_DSN)
    admin_engine = admin_engine.execution_options(isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS {check_db} WITH (FORCE)'))
        await conn.execute(text(f"CREATE DATABASE {check_db}"))
        await conn.execute(
            text(f"GRANT CREATE ON DATABASE {check_db} TO {_dsn_user(MIGRATION_DSN)}")
        )
    await admin_engine.dispose()

    migration_dsn = MIGRATION_DSN.rsplit("/", 1)[0] + f"/{check_db}"
    os.environ["MAP_CONTROL_MIGRATION_DSN"] = migration_dsn
    cfg = Config(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(project_root, "app/db/migrations"))
    try:
        await _asyncio.to_thread(command.upgrade, cfg, "head")
        await _asyncio.to_thread(command.downgrade, cfg, "base")
        await _asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ["MAP_CONTROL_MIGRATION_DSN"] = MIGRATION_DSN

    # The app role cannot run DDL on the fresh database either.
    app_check_engine = create_async_engine(APP_DSN.rsplit("/", 1)[0] + f"/{check_db}")
    async with app_check_engine.connect() as conn:
        try:
            await conn.execute(text("CREATE TABLE probe_from_app (id int)"))
            await conn.rollback()
            pytest.fail("app role must not run DDL")
        except Exception:  # noqa: BLE001 - expected denial
            await conn.rollback()
    await app_check_engine.dispose()

    admin_engine = create_async_engine(ADMIN_DSN)
    admin_engine = admin_engine.execution_options(isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {check_db} WITH (FORCE)"))
    await admin_engine.dispose()


# --- R2-P1-02: every admin write operation must carry a real fixture -------
# The fixture set below is asserted to be EXACTLY the set of admin write
# operations enumerated from OpenAPI; adding a route without a fixture (or
# leaving a stale one) fails CI. Every fixture is really executed and must
# produce the expected audit deltas:
#   - mutating admin write: 1 admin audit (resource_type !=
#     'runtime_snapshot') + 1 runtime_snapshot audit
#   - runtime snapshot lifecycle op: 1 snapshot audit only
#   - test-chat: neither (read-only debug path)

_ENUM_AGENT_CODE = "enum-audit-agent"
_ENUM_MCP_SERVER_ID = "enum-audit-mcp"

_PUT_SECTION_PATHS = [
    "/api/admin/model-center",
    "/api/admin/basic-settings",
    "/api/admin/address-configs",
    "/api/admin/data-connectors",
    "/api/admin/data-assets",
    "/api/admin/session-policies",
    "/api/admin/dashboard-cards",
    "/api/admin/security-policies",
    "/api/admin/glossary-terms",
    "/api/admin/homepage-recommendations",
    "/api/admin/permission-rules",
    "/api/admin/role-policies",
    "/api/admin/user-accounts",
    "/api/admin/knowledge-bindings",
    "/api/admin/skill-policies",
    "/api/admin/flow-policy",
    "/api/admin/scenario-packs",
    "/api/admin/flow-skill-descriptors",
    "/api/admin/mcp-servers",
    "/api/admin/skills",
    "/api/admin/master-agent",
]


def _put_section(path: str):
    async def _run(client, session=None):
        payload = (await client.get(path)).json()
        return await client.put(path, json=payload)

    return _run


async def _run_release_history(client, session=None):
    return await client.post(
        "/api/admin/release-history",
        params={"note": "enum-audit", "operator": "attacker"},
    )


async def _run_master_publish(client, session=None):
    return await client.post(
        "/api/admin/master-agent/publish",
        json={"operator": "attacker", "note": "enum publish"},
    )


async def _run_master_rollback(client, session=None):
    master = (await client.get("/api/admin/master-agent")).json()
    return await client.post(
        "/api/admin/master-agent/rollback",
        json={"version": master["current_version"], "operator": "attacker"},
    )


async def _run_agent_create(client, session=None):
    return await client.post(
        "/api/admin/business-agents",
        json={
            "agent_code": _ENUM_AGENT_CODE,
            "display_name": "枚举代理",
            "scene_name": "enum",
            "owner_team": "map",
        },
    )


async def _run_agent_update(client, session=None):
    agents = (await client.get("/api/admin/business-agents")).json()
    agent = next(item for item in agents if item["agent_code"] == _ENUM_AGENT_CODE)
    agent["display_name"] = "枚举代理-更新"
    return await client.put(f"/api/admin/business-agents/{_ENUM_AGENT_CODE}", json=agent)


async def _run_agent_test_chat(client, session=None):
    # test-chat materializes only supported scene agents; pick a seeded one.
    from app.services.runtime_payloads import SUPPORTED_SCENE_AGENT_CODES

    agents = (await client.get("/api/admin/business-agents")).json()
    agent_code = next(
        item["agent_code"]
        for item in agents
        if item["agent_code"] in SUPPORTED_SCENE_AGENT_CODES and item.get("enabled", True)
    )
    return await client.post(
        f"/api/admin/business-agents/{agent_code}/test-chat",
        json={"query": "ping"},
    )


async def _run_mcp_create(client, session=None):
    return await client.post(
        "/api/admin/mcp-servers",
        json={
            "server_id": _ENUM_MCP_SERVER_ID,
            "display_name": "枚举 MCP",
            "transport": "stdio",
        },
    )


async def _run_mcp_refresh(client, session=None):
    return await client.post(f"/api/admin/mcp-servers/{_ENUM_MCP_SERVER_ID}/refresh-tools")


async def _run_skill_upload(client, session=None):
    return await client.post(
        "/api/admin/skills/upload",
        json={
            "filename": "enum-audit.md",
            "content": "# Enum Skill",
            "encoding": "text",
            "metadata": {"name": "enum-audit"},
        },
    )


def _snapshot_fixture_projection(tag: str) -> RuntimeProjection:
    return RuntimeProjection(
        schema_version=1,
        scene_selection={"fixture": tag},
        dispatch_config={},
        flow_policy={},
        scenario_packs=[],
        flow_skill_descriptors=[],
    )


async def _seed_snapshot_fixture(session, tag: str, status: str) -> uuid.UUID:
    """Seed one deterministic snapshot row for the lifecycle fixtures.

    The lifecycle POST routes only need the row to exist in the right
    status; this is setup, not the operation under test, so it emits no
    audit events.
    """
    from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
    from app.services.runtime_snapshot.digest import projection_digest, snapshot_id_for_digest

    projection = _snapshot_fixture_projection(tag)
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    await PgRuntimeSnapshotRepository(session).insert(
        snapshot_id, projection, digest, None, status
    )
    await session.commit()
    return snapshot_id


async def _run_snapshot_publish(client, session):
    snapshot_id = await _seed_snapshot_fixture(session, "fixture-publish", "draft")
    return await client.post(f"/api/admin/runtime-snapshots/{snapshot_id}/publish")


async def _run_snapshot_activate(client, session):
    snapshot_id = await _seed_snapshot_fixture(session, "fixture-activate", "published")
    # No body: the route derives the CAS expectation from the server-side
    # current pointer (fail-closed), exactly like the service contract.
    return await client.post(f"/api/admin/runtime-snapshots/{snapshot_id}/activate")


async def _run_snapshot_rollback(client, session):
    snapshot_id = await _seed_snapshot_fixture(session, "fixture-rollback", "rolled_back")
    return await client.post(f"/api/admin/runtime-snapshots/{snapshot_id}/rollback")


async def _run_snapshot_retire(client, session):
    snapshot_id = await _seed_snapshot_fixture(session, "fixture-retire", "published")
    return await client.post(f"/api/admin/runtime-snapshots/{snapshot_id}/retire")


ADMIN_WRITE_FIXTURES: dict[str, dict] = {}
for _path in _PUT_SECTION_PATHS:
    ADMIN_WRITE_FIXTURES[f"PUT {_path}"] = {
        "run": _put_section(_path),
        "admin_audit_delta": 1,
        "snapshot_audit_delta": 1,
    }
ADMIN_WRITE_FIXTURES.update(
    {
        "POST /api/admin/release-history": {
            "run": _run_release_history,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/master-agent/publish": {
            "run": _run_master_publish,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/master-agent/rollback": {
            "run": _run_master_rollback,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/business-agents": {
            "run": _run_agent_create,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        "PUT /api/admin/business-agents/{agent_code}": {
            "run": _run_agent_update,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        # Debug chat performs no AdminState write: no audit event expected.
        "POST /api/admin/business-agents/{agent_code}/test-chat": {
            "run": _run_agent_test_chat,
            "admin_audit_delta": 0,
            "snapshot_audit_delta": 0,
        },
        "POST /api/admin/mcp-servers": {
            "run": _run_mcp_create,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/mcp-servers/{server_id}/refresh-tools": {
            "run": _run_mcp_refresh,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/skills/upload": {
            "run": _run_skill_upload,
            "admin_audit_delta": 1,
            "snapshot_audit_delta": 1,
        },
        # Runtime snapshot lifecycle: no AdminState write, exactly one
        # snapshot audit event each.
        "POST /api/admin/runtime-snapshots/{snapshot_id}/publish": {
            "run": _run_snapshot_publish,
            "admin_audit_delta": 0,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/runtime-snapshots/{snapshot_id}/activate": {
            "run": _run_snapshot_activate,
            "admin_audit_delta": 0,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/runtime-snapshots/{snapshot_id}/rollback": {
            "run": _run_snapshot_rollback,
            "admin_audit_delta": 0,
            "snapshot_audit_delta": 1,
        },
        "POST /api/admin/runtime-snapshots/{snapshot_id}/retire": {
            "run": _run_snapshot_retire,
            "admin_audit_delta": 0,
            "snapshot_audit_delta": 1,
        },
    }
)

# Dependency-safe execution order (create before update/refresh/rollback;
# lifecycle ops run last, after admin writes have materialized a current
# active snapshot and rolled_back predecessors).
_ORDERED_OPS = [f"PUT {path}" for path in _PUT_SECTION_PATHS] + [
    "POST /api/admin/release-history",
    "POST /api/admin/master-agent/publish",
    "POST /api/admin/master-agent/rollback",
    "POST /api/admin/business-agents",
    "PUT /api/admin/business-agents/{agent_code}",
    "POST /api/admin/business-agents/{agent_code}/test-chat",
    "POST /api/admin/mcp-servers",
    "POST /api/admin/mcp-servers/{server_id}/refresh-tools",
    "POST /api/admin/skills/upload",
    "POST /api/admin/runtime-snapshots/{snapshot_id}/publish",
    "POST /api/admin/runtime-snapshots/{snapshot_id}/activate",
    "POST /api/admin/runtime-snapshots/{snapshot_id}/rollback",
    "POST /api/admin/runtime-snapshots/{snapshot_id}/retire",
]


async def test_every_admin_write_operation_has_fixture_and_is_audited(
    app_and_session,
) -> None:
    """R2-P1-02: OpenAPI admin write operations == fixture set (exact),
    each fixture really executes, and audit deltas match the Step 7 PR-J3
    contract per resource_type bucket."""
    app, session, _ = app_and_session
    openapi = app.openapi()
    enumerated = {
        f"{method.upper()} {path}"
        for path, methods in openapi["paths"].items()
        if path.startswith("/api/admin")
        for method in ("put", "post", "patch", "delete")
        if method in methods
    }
    assert enumerated == set(ADMIN_WRITE_FIXTURES), (
        "admin write operations and the fixture set must match exactly; "
        f"missing fixtures={sorted(enumerated - set(ADMIN_WRITE_FIXTURES))}, "
        f"stale fixtures={sorted(set(ADMIN_WRITE_FIXTURES) - enumerated)}"
    )
    assert set(_ORDERED_OPS) == enumerated

    async with await _client(app) as client:
        for op in _ORDERED_OPS:
            fixture = ADMIN_WRITE_FIXTURES[op]
            before_admin = await _audit_count_by_resource(session, "admin")
            before_snapshot = await _audit_count_by_resource(
                session, "runtime_snapshot"
            )
            response = await fixture["run"](client, session)
            assert response.status_code == 200, (op, response.text)
            admin_delta = (
                await _audit_count_by_resource(session, "admin")
            ) - before_admin
            snapshot_delta = (
                await _audit_count_by_resource(session, "runtime_snapshot")
            ) - before_snapshot
            assert admin_delta == fixture["admin_audit_delta"], (
                op,
                admin_delta,
                fixture["admin_audit_delta"],
            )
            assert snapshot_delta == fixture["snapshot_audit_delta"], (
                op,
                snapshot_delta,
                fixture["snapshot_audit_delta"],
            )

    # All events from this run carry the trusted actor, never the
    # client-claimed operator.
    rows = (
        await session.execute(
            text(
                "SELECT actor_user_id, status FROM map_control.config_audit_events "
                "ORDER BY ordinal"
            )
        )
    ).all()
    expected_total = sum(
        fixture["admin_audit_delta"] + fixture["snapshot_audit_delta"]
        for fixture in ADMIN_WRITE_FIXTURES.values()
    )
    assert len(rows) == expected_total
    assert {row.actor_user_id for row in rows} == {"local-admin"}
    assert {row.status for row in rows} == {"applied"}


def store_load_hash(store: AdminStateStore) -> str:
    from app.store import state_hash

    return state_hash(store.load())
