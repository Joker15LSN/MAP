"""Admin runtime snapshot lifecycle routes (Step 7 PR-J4)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from conftest import seed_pg_admin_state
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import projection_digest, snapshot_id_for_digest
from app.services.runtime_snapshot.schemas import RuntimeProjection
from app.settings import Settings

pytestmark = pytest.mark.asyncio

WORKSPACE = "00000000-0000-0000-0000-000000000001"


def _projection(tag: str) -> RuntimeProjection:
    return RuntimeProjection(
        schema_version=1,
        scene_selection={"tag": tag},
        dispatch_config={},
        flow_policy={},
        scenario_packs=[],
        flow_skill_descriptors=[],
    )


@pytest_asyncio.fixture
async def app_and_factory(_engine, session, tmp_path):
    # ``session`` is requested only for its per-test TRUNCATE isolation.
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=str(tmp_path / "admin_state.json"),
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
    async with factory() as _seed_session:
        await seed_pg_admin_state(_seed_session)
    return app, factory


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_snapshot(
    factory,
    tag: str,
    status: str,
    *,
    seed_current: bool = False,
) -> tuple[uuid.UUID, str]:
    """Insert one deterministic snapshot; optionally make it current."""
    projection = _projection(tag)
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    async with factory() as session:
        repo = PgRuntimeSnapshotRepository(session)
        await repo.insert(snapshot_id, projection, digest, None, status)
        if seed_current:
            await repo.activate(snapshot_id, None)
        await session.commit()
    return snapshot_id, digest


async def test_current_and_get_return_record_and_404(app_and_factory) -> None:
    app, factory = app_and_factory
    snapshot_id, digest = await _seed_snapshot(factory, "get", "published")

    async with await _client(app) as client:
        missing_current = await client.get("/api/admin/runtime-snapshots/current")
        assert missing_current.status_code == 404
        assert missing_current.headers["X-MAP-Error-Code"] == "SNAPSHOT_NOT_FOUND"

        missing = await client.get(
            f"/api/admin/runtime-snapshots/{uuid.uuid4()}"
        )
        assert missing.status_code == 404
        assert missing.headers["X-MAP-Error-Code"] == "SNAPSHOT_NOT_FOUND"

        got = await client.get(f"/api/admin/runtime-snapshots/{snapshot_id}")
        assert got.status_code == 200
        body = got.json()
        assert body["id"] == str(snapshot_id)
        assert body["digest"] == digest
        assert body["status"] == "published"


async def test_concurrent_activate_same_expected_digest_one_wins(
    app_and_factory,
) -> None:
    """Two activations with the same expected pointer digest: exactly one
    may win; the loser gets 409 SNAPSHOT_CONCURRENT_MODIFICATION."""
    app, factory = app_and_factory
    _current_id, current_digest = await _seed_snapshot(
        factory, "concurrent-current", "published", seed_current=True
    )
    target_id, target_digest = await _seed_snapshot(factory, "concurrent-target", "published")

    async with await _client(app) as client:
        async def _activate():
            return await client.post(
                f"/api/admin/runtime-snapshots/{target_id}/activate",
                json={"expected_current_digest": current_digest},
            )

        first, second = await asyncio.gather(_activate(), _activate())

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    winner = second if first.status_code == 409 else first
    assert loser.headers["X-MAP-Error-Code"] == "SNAPSHOT_CONCURRENT_MODIFICATION"
    assert winner.json()["id"] == str(target_id)
    assert winner.json()["digest"] == target_digest


async def test_retire_active_is_409_state_conflict(app_and_factory) -> None:
    app, factory = app_and_factory
    active_id, _ = await _seed_snapshot(
        factory, "retire-active", "published", seed_current=True
    )

    async with await _client(app) as client:
        response = await client.post(
            f"/api/admin/runtime-snapshots/{active_id}/retire"
        )
    assert response.status_code == 409
    assert response.headers["X-MAP-Error-Code"] == "SNAPSHOT_STATE_CONFLICT"


async def test_publish_non_draft_is_409_state_conflict(app_and_factory) -> None:
    app, factory = app_and_factory
    published_id, _ = await _seed_snapshot(factory, "publish-non-draft", "published")

    async with await _client(app) as client:
        response = await client.post(
            f"/api/admin/runtime-snapshots/{published_id}/publish"
        )
    assert response.status_code == 409
    assert response.headers["X-MAP-Error-Code"] == "SNAPSHOT_STATE_CONFLICT"


async def test_lifecycle_missing_snapshot_is_404(app_and_factory) -> None:
    app, factory = app_and_factory
    _active_id, _active_digest = await _seed_snapshot(
        factory, "missing-target-current", "published", seed_current=True
    )
    missing_id = uuid.uuid4()

    async with await _client(app) as client:
        publish = await client.post(
            f"/api/admin/runtime-snapshots/{missing_id}/publish"
        )
        activate = await client.post(
            f"/api/admin/runtime-snapshots/{missing_id}/activate",
            json={"expected_current_digest": None},
        )
        rollback = await client.post(
            f"/api/admin/runtime-snapshots/{missing_id}/rollback"
        )
        retire = await client.post(
            f"/api/admin/runtime-snapshots/{missing_id}/retire"
        )

    for response in (publish, activate, rollback, retire):
        assert response.status_code == 404, response.text
        assert response.headers["X-MAP-Error-Code"] == "SNAPSHOT_NOT_FOUND"


async def test_admin_write_guard_protects_snapshot_routes(
    app_and_factory, tmp_path
) -> None:
    app, _factory = app_and_factory
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file=str(tmp_path / "other_state.json"),
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
        store=None,
        core_client=None,
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    headers = {
        "X-UserId": "u-member",
        "X-User-Roles": "member",
        "X-Trusted-Proxy-Secret": "s3cret",
    }
    async with await _client(other_app) as client:
        current = await client.get(
            "/api/admin/runtime-snapshots/current", headers=headers
        )
        publish = await client.post(
            f"/api/admin/runtime-snapshots/{uuid.uuid4()}/publish", headers=headers
        )

    assert current.status_code == 403
    assert publish.status_code == 403
