"""Public /api/v1/runs* route contract tests (PR-C).

The route is only a protocol adapter: the same RunApplication interface is
exercised with an in-memory store so the tests stay deterministic.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_run_application, get_runtime_snapshots
from app.main import create_app
from app.runs import InMemoryCoreRunStream, InMemoryRunStore, RunApplication, RunWorker
from app.runs.domain import CoreOutcome, RunCommand
from app.services.runtime_snapshot.adapters.memory import (
    InMemoryRuntimeSnapshotRepository,
)
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.schemas import RuntimeProjection
from app.services.runtime_snapshot.service import RuntimeSnapshotService
from app.settings import Settings

DEFAULT_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _runtime_projection() -> RuntimeProjection:
    return RuntimeProjection(
        schema_version=1,
        scene_selection={},
        dispatch_config={},
        flow_policy={},
        scenario_packs=[],
        flow_skill_descriptors=[],
    )


def _command() -> RunCommand:
    return RunCommand(
        kind="conversation_turn",
        payload={"query": "hello"},
        snapshot={"runtime": "v1"},
    )


def _create_body() -> dict:
    return {
        "command": {
            "kind": "conversation_turn",
            "payload": {"query": "hello"},
            "snapshot": {"runtime": "v1"},
        }
    }


@pytest.fixture()
async def client():
    store = InMemoryRunStore()
    application = RunApplication(store)
    app = create_app(
        settings=Settings(auth_mode="dev")
    )

    projection = _runtime_projection()
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    snapshot_repo = InMemoryRuntimeSnapshotRepository()
    await snapshot_repo.insert(snapshot_id, projection, digest, None, "published")
    await snapshot_repo.activate(snapshot_id, None)

    app.dependency_overrides[get_run_application] = lambda: application
    app.dependency_overrides[get_runtime_snapshots] = lambda: RuntimeSnapshotService(
        app.state.store, snapshot_repo
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client, application, store


async def test_create_get_cancel_run_path(client) -> None:
    http, _application, _store = client
    response = await http.post(
        "/api/v1/runs",
        json=_create_body(),
        headers={"Idempotency-Key": "route-k-1"},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"run_id", "status", "replayed"}
    assert body["status"] == "queued"

    run_id = body["run_id"]
    fetched = await http.get(f"/api/v1/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id
    assert fetched.json()["runtime_snapshot_id"] is not None
    assert fetched.json()["runtime_snapshot_digest"] is not None

    cancelled = await http.post(
        f"/api/v1/runs/{run_id}:cancel", json={"reason": "stop"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["accepted"] is True
    assert cancelled.json()["status"] == "queued"


async def test_create_replay_and_conflict_envelopes(client) -> None:
    http, _application, _store = client
    first = await http.post(
        "/api/v1/runs", json=_create_body(), headers={"Idempotency-Key": "route-k-2"}
    )
    replay = await http.post(
        "/api/v1/runs", json=_create_body(), headers={"Idempotency-Key": "route-k-2"}
    )
    assert first.json()["run_id"] == replay.json()["run_id"]
    assert replay.json()["replayed"] is True

    changed = _create_body()
    changed["command"]["payload"] = {"query": "changed"}
    conflict = await http.post(
        "/api/v1/runs", json=changed, headers={"Idempotency-Key": "route-k-2"}
    )
    assert conflict.status_code == 409
    assert set(conflict.json()) == {"code", "message", "details", "request_id"}
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


async def test_create_requires_idempotency_key(client) -> None:
    http, _application, _store = client
    response = await http.post("/api/v1/runs", json=_create_body())
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


async def test_run_not_found_is_enveloped_404(client) -> None:
    http, _application, _store = client
    response = await http.get(f"/api/v1/runs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


async def test_events_replay_uses_sse_frames(client) -> None:
    http, application, store = client
    created = await application.create_run(
        workspace_id=DEFAULT_WORKSPACE,
        principal_id="local-admin",
        conversation_id=None,
        command=_command(),
        runtime_snapshot_id=uuid.uuid4(),
        runtime_snapshot_digest="b" * 64,
        idempotency_key="route-k-3",
        idempotency_body_hash="h-3",
    )
    await RunWorker(
        store, InMemoryCoreRunStream([CoreOutcome(status="completed")])
    ).run_once(worker_id="w-1")

    response = await http.get(f"/api/v1/runs/{created.run_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert "event: run.started" in text
    assert "event: run.completed" in text
    ids = [line for line in text.splitlines() if line.startswith("id: ")]
    assert ids == ["id: 1", "id: 2", "id: 3", "id: 4"]
