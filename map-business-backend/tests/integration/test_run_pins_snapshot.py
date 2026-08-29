"""Step 7 PR-J5: runs pin the runtime snapshot current pointer (PG).

The public run creation route must resolve the server-side current
snapshot and store its id/digest on the run. Replays return the stored
snapshot; no current pointer is a 503 fail-closed; activating a new
snapshot never rewrites historical runs.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_run_application
from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.runs import PgRunStore, RunApplication
from app.schemas import AdminState
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.schemas import (
    RuntimeProjection,
    build_runtime_projection,
)
from app.settings import Settings

pytestmark = pytest.mark.asyncio

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _body() -> dict:
    return {
        "command": {
            "kind": "conversation_turn",
            "payload": {"query": "hello"},
            "snapshot": {},
        }
    }


def _projection(tag: str) -> RuntimeProjection:
    projection = build_runtime_projection(AdminState.default())
    return projection.model_copy(update={"scene_selection": {"tag": tag}})


@pytest.fixture()
async def http_client(_engine, session, tmp_path):
    del session  # test isolation side effect is enough
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=str(tmp_path / "admin_state.json"),
            default_workspace_id=str(WORKSPACE),
        )
    )

    async def _override_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_run_application] = lambda: RunApplication(
        PgRunStore(factory)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, factory


async def _seed_current(
    factory, projection: RuntimeProjection
) -> tuple[uuid.UUID, str]:
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    async with factory() as s:
        repo = PgRuntimeSnapshotRepository(s)
        await repo.insert(snapshot_id, projection, digest, None, "published")
        await repo.activate(snapshot_id, None)
        await s.commit()
    return snapshot_id, digest


async def _clear_current(factory, snapshot_id: uuid.UUID) -> None:
    async with factory() as s:
        await s.execute(
            text("DELETE FROM map_control.runtime_snapshot_current WHERE id = 1")
        )
        await s.execute(
            text(
                "UPDATE map_control.runtime_snapshots SET status = 'retired' "
                "WHERE id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        await s.commit()


async def test_run_pins_snapshot(http_client) -> None:
    client, factory = http_client

    snapshot_a_id, snapshot_a_digest = await _seed_current(
        factory, _projection("A")
    )

    first = await client.post(
        "/api/v1/runs", json=_body(), headers={"Idempotency-Key": "pin-1"}
    )
    assert first.status_code == 201
    run_id = first.json()["run_id"]

    fetched = await client.get(f"/api/v1/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["runtime_snapshot_id"] == str(snapshot_a_id)
    assert fetched.json()["runtime_snapshot_digest"] == snapshot_a_digest

    replay = await client.post(
        "/api/v1/runs", json=_body(), headers={"Idempotency-Key": "pin-1"}
    )
    assert replay.status_code == 201
    assert replay.json()["run_id"] == run_id
    replay_fetched = await client.get(f"/api/v1/runs/{run_id}")
    assert replay_fetched.json()["runtime_snapshot_id"] == str(snapshot_a_id)
    assert replay_fetched.json()["runtime_snapshot_digest"] == snapshot_a_digest

    # Fail closed: once the pointer is gone, new runs are rejected with 503.
    await _clear_current(factory, snapshot_a_id)
    unavailable = await client.post(
        "/api/v1/runs", json=_body(), headers={"Idempotency-Key": "pin-none"}
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "RUNTIME_SNAPSHOT_UNAVAILABLE"

    # A new activation must NOT rewrite historical runs.
    snapshot_b_id, snapshot_b_digest = await _seed_current(
        factory, _projection("B")
    )
    old_fetched = await client.get(f"/api/v1/runs/{run_id}")
    assert old_fetched.status_code == 200
    assert old_fetched.json()["runtime_snapshot_id"] == str(snapshot_a_id)
    assert old_fetched.json()["runtime_snapshot_digest"] == snapshot_a_digest

    second = await client.post(
        "/api/v1/runs", json=_body(), headers={"Idempotency-Key": "pin-2"}
    )
    assert second.status_code == 201
    second_run_id = second.json()["run_id"]
    second_fetched = await client.get(f"/api/v1/runs/{second_run_id}")
    assert second_fetched.status_code == 200
    assert second_fetched.json()["runtime_snapshot_id"] == str(snapshot_b_id)
    assert second_fetched.json()["runtime_snapshot_digest"] == snapshot_b_digest
