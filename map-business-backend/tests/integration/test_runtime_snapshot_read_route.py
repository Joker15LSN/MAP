"""Internal runtime-config snapshot read route (Step 7 PR-J1)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from conftest import seed_pg_admin_state
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identity import AuthMode
from app.core.service_identity import ServiceCredential
from app.db.session import get_db_session
from app.main import create_app
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import projection_digest
from app.services.runtime_snapshot.schemas import RuntimeProjection
from app.settings import Settings

pytestmark = pytest.mark.asyncio

SNAP_CRED = ServiceCredential(
    key_id="core-reader-v1",
    token="svc-token-snapshot-reader",
    service_name="core",
    audience="map-bff",
    scopes=("runtime-config.snapshots.read",),
)

WORKSPACE = "00000000-0000-0000-0000-000000000001"


def _projection() -> RuntimeProjection:
    return RuntimeProjection(
        schema_version=1,
        scene_selection={"enabled_agent_codes": {}},
        dispatch_config={"scene_agent_configs": {}},
        flow_policy={},
        scenario_packs=[],
        flow_skill_descriptors=[],
    )


@pytest_asyncio.fixture
async def app_and_factory(_engine, tmp_path):
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            default_workspace_id=WORKSPACE,
            service_credentials=(SNAP_CRED,),
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
    async with factory() as _seed_session:
        await seed_pg_admin_state(_seed_session)
    return app, factory


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _insert_snapshot(
    factory,
    *,
    digest: str | None = None,
    status: str = "published",
    projection: RuntimeProjection | None = None,
) -> tuple[uuid.UUID, str]:
    projection = projection or _projection()
    digest = digest or projection_digest(projection)
    async with factory() as s:
        repo = PgRuntimeSnapshotRepository(s)
        snapshot_id = uuid.uuid5(
            uuid.UUID("7d9d4f2a-1e5b-4e6c-8a2f-3b0c9d1f5a6e"), digest
        )
        await repo.insert(snapshot_id, projection, digest, None, status)
        await s.commit()
    return snapshot_id, digest


async def test_snapshot_read_returns_etag_and_no_store(session, app_and_factory) -> None:
    app, factory = app_and_factory
    snapshot_id, digest = await _insert_snapshot(factory)
    async with await _client(app) as client:
        response = await client.get(
            f"/internal/v1/runtime-config-snapshots/{snapshot_id}",
            headers={"Authorization": f"Bearer {SNAP_CRED.token}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(snapshot_id)
    assert body["digest"] == digest
    assert body["projection"]["schema_version"] == 1
    assert response.headers["ETag"] == f'"{digest}"'
    assert response.headers["X-MAP-Snapshot-Digest"] == digest
    assert response.headers["Cache-Control"] == "no-store"


async def test_snapshot_read_requires_scope(app_and_factory) -> None:
    app, _factory = app_and_factory
    limited = ServiceCredential(
        key_id="core-limited-v1",
        token="svc-token-snapshot-limited",
        service_name="core",
        audience="map-bff",
        scopes=("internal.ping",),
    )
    app.state.settings = Settings(
        auth_mode=AuthMode.DEV,
        default_workspace_id=WORKSPACE,
        service_credentials=(limited,),
    )
    async with await _client(app) as client:
        response = await client.get(
            f"/internal/v1/runtime-config-snapshots/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {limited.token}"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


async def test_snapshot_read_missing_or_draft_is_404(session, app_and_factory) -> None:
    app, factory = app_and_factory
    async with await _client(app) as client:
        missing = await client.get(
            f"/internal/v1/runtime-config-snapshots/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {SNAP_CRED.token}"},
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "SNAPSHOT_NOT_FOUND"

        draft_id, _ = await _insert_snapshot(factory, status="draft")
        draft = await client.get(
            f"/internal/v1/runtime-config-snapshots/{draft_id}",
            headers={"Authorization": f"Bearer {SNAP_CRED.token}"},
        )
        assert draft.status_code == 404
        assert draft.json()["code"] == "SNAPSHOT_NOT_FOUND"


async def test_snapshot_read_digest_mismatch_is_fail_closed_500(session, app_and_factory) -> None:
    app, factory = app_and_factory
    snapshot_id, _ = await _insert_snapshot(factory, digest="f" * 64)
    async with await _client(app) as client:
        response = await client.get(
            f"/internal/v1/runtime-config-snapshots/{snapshot_id}",
            headers={"Authorization": f"Bearer {SNAP_CRED.token}"},
        )
    assert response.status_code == 500
    assert response.json()["code"] == "SNAPSHOT_DIGEST_MISMATCH"
