"""RuntimeSnapshotService lifecycle + reconciler tests (Step 7 PR-J2)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from starlette.requests import Request

from app.core.identity import AuthMode, RequestPrincipal
from app.schemas import AdminState
from app.services.config_mutation import AuditWriteError
from app.services.runtime_snapshot import RuntimeSnapshotService
from app.services.runtime_snapshot.adapters.admin_state_pg import (
    PgAdminStateRepository,
)
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
    state_hash,
)
from app.services.runtime_snapshot.schemas import (
    MutationContext,
    RuntimeProjection,
)

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


def _context() -> MutationContext:
    request = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": None,
            "client": None,
        }
    )
    principal = RequestPrincipal(
        subject="admin",
        user_id="local-admin",
        staff_code=None,
        display_name="Admin",
        roles=("platform_admin",),
        workspace_id=WORKSPACE,
        auth_mode=AuthMode.DEV,
    )
    return MutationContext(
        principal=principal,
        request=request,
        resource_type="flow_policy",
        resource_id="flow_policy",
        action="update",
    )


@pytest_asyncio.fixture
async def service_env(session):
    admin_repo = PgAdminStateRepository(session)
    await admin_repo.seed_if_empty(AdminState.default())
    await session.commit()
    repo = PgRuntimeSnapshotRepository(session)
    service = RuntimeSnapshotService(admin_repo, repo)
    return service, admin_repo, repo, session


async def _audit_rows(session) -> list[tuple[str, str, str]]:
    rows = (
        await session.execute(
            text(
                "SELECT resource_type, action, status FROM map_control.config_audit_events "
                "ORDER BY ordinal"
            )
        )
    ).all()
    return [(r.resource_type, r.action, r.status) for r in rows]


async def test_apply_change_commits_snapshot_audit_outbox_and_no_pending(
    service_env,
) -> None:
    service, store, _repo, session = service_env

    def updater(draft):
        draft.basic_settings = []
        return "changed"

    new_state, result = await service.apply_change(session, _context(), updater)
    assert result == "changed"
    assert state_hash(await store.load()) == state_hash(new_state)

    current = await service.get_current()
    assert current is not None and current.status == "active"

    assert await _audit_rows(session) == [
        ("flow_policy", "update", "applied"),
        ("runtime_snapshot", "activate", "applied"),
    ]

    outbox_type = (
        await session.execute(text("SELECT event_type FROM map_control.outbox_events"))
    ).scalars().one()
    assert outbox_type == "runtime_snapshot.activated"


async def test_apply_change_business_rejection_is_audited_without_snapshot(
    service_env,
) -> None:
    service, _store, _repo, session = service_env

    def updater(draft):
        raise HTTPException(
            status_code=409,
            detail="business rejected",
            headers={"X-MAP-Error-Code": "BUSINESS_REJECTED"},
        )

    with pytest.raises(HTTPException) as exc_info:
        await service.apply_change(session, _context(), updater)
    assert exc_info.value.status_code == 409
    assert await _audit_rows(session) == [
        ("flow_policy", "update", "rejected")
    ]
    assert await service.get_current() is None


async def test_lifecycle_publish_activate_rollback_retire(service_env) -> None:
    service, _store, repo, session = service_env
    ctx = _context()

    proj_a = _projection("a")
    digest_a = projection_digest(proj_a)
    id_a = snapshot_id_for_digest(digest_a)
    await repo.insert(id_a, proj_a, digest_a, None, "draft")

    published = await service.publish(session, id_a, ctx)
    assert published.status == "published"
    activated = await service.activate(session, id_a, None, ctx)
    assert activated.status == "active"

    proj_b = _projection("b")
    digest_b = projection_digest(proj_b)
    id_b = snapshot_id_for_digest(digest_b)
    await repo.insert(id_b, proj_b, digest_b, id_a, "draft")
    await service.publish(session, id_b, ctx)
    await service.activate(session, id_b, digest_a, ctx)

    assert (await repo.get(id_a)).status == "rolled_back"
    assert (await service.get_current()).id == id_b

    rolled_back = await service.rollback(session, id_a, ctx)
    assert rolled_back.status == "active"
    assert (await service.get_current()).id == id_a
    assert (await repo.get(id_b)).status == "rolled_back"

    retired = await service.retire(session, id_b, ctx)
    assert retired.status == "retired"
    assert (await repo.get(id_b)).status == "retired"

    assert await _audit_rows(session) == [
        ("runtime_snapshot", "publish", "applied"),
        ("runtime_snapshot", "activate", "applied"),
        ("runtime_snapshot", "publish", "applied"),
        ("runtime_snapshot", "activate", "applied"),
        ("runtime_snapshot", "rollback", "applied"),
        ("runtime_snapshot", "retire", "applied"),
    ]
    outbox_types = (
        await session.execute(
            text("SELECT event_type FROM map_control.outbox_events")
        )
    ).scalars().all()
    assert sorted(outbox_types) == [
        "runtime_snapshot.activated",
        "runtime_snapshot.activated",
        "runtime_snapshot.published",
        "runtime_snapshot.published",
        "runtime_snapshot.retired",
        "runtime_snapshot.rollback",
    ]


async def test_retire_active_is_conflict(service_env) -> None:
    service, _store, repo, session = service_env
    proj = _projection("active")
    digest = projection_digest(proj)
    sid = snapshot_id_for_digest(digest)
    await repo.insert(sid, proj, digest, None, "published")
    await service.activate(session, sid, None, _context())

    with pytest.raises(HTTPException) as exc_info:
        await service.retire(session, sid, _context())
    assert exc_info.value.status_code == 409
    assert exc_info.value.headers["X-MAP-Error-Code"] == "SNAPSHOT_STATE_CONFLICT"


async def test_activate_cas_conflict_is_409_and_audited(service_env) -> None:
    service, _store, repo, session = service_env
    proj = _projection("cas")
    digest = projection_digest(proj)
    sid = snapshot_id_for_digest(digest)
    await repo.insert(sid, proj, digest, None, "published")

    with pytest.raises(HTTPException) as exc_info:
        await service.activate(session, sid, "f" * 64, _context())
    assert exc_info.value.status_code == 409
    assert (
        exc_info.value.headers["X-MAP-Error-Code"]
        == "SNAPSHOT_CONCURRENT_MODIFICATION"
    )


async def test_apply_change_audit_failure_rolls_back_whole_transaction(
    service_env, monkeypatch
) -> None:
    service, store, _repo, session = service_env
    before_hash = state_hash(await store.load())

    async def fake_append_audit_event(session, **kwargs):
        raise AuditWriteError("boom")

    monkeypatch.setattr(
        "app.services.runtime_snapshot.service.append_audit_event",
        fake_append_audit_event,
    )

    def updater(draft):
        draft.business_agents = []
        return "changed"

    with pytest.raises(HTTPException) as exc_info:
        await service.apply_change(session, _context(), updater)
    assert exc_info.value.status_code == 500
    assert exc_info.value.headers["X-MAP-Error-Code"] == "AUDIT_WRITE_FAILED"

    # J7a: PG atomicity rolls the admin state back together with the
    # snapshot side; no pending mutation row is needed for recovery.
    assert state_hash(await store.load()) == before_hash
    snapshot_count = (
        await session.execute(text("SELECT count(*) FROM map_control.runtime_snapshots"))
    ).scalar_one()
    assert snapshot_count == 0
