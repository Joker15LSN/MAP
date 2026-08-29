"""RuntimeSnapshotService lifecycle + reconciler tests (Step 7 PR-J2)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.core.identity import AuthMode, RequestPrincipal
from app.db.models import RuntimeSnapshotMutation
from app.services.config_mutation import AuditWriteError
from app.services.runtime_snapshot import RuntimeSnapshotService
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.schemas import (
    MutationContext,
    RuntimeProjection,
    build_runtime_projection,
)
from app.services.runtime_snapshot.service import (
    reconcile_runtime_snapshot_mutations,
)
from app.store import AdminStateStore, state_hash

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
async def service_env(session, tmp_path):
    store = AdminStateStore(str(tmp_path / "admin_state.json"))
    repo = PgRuntimeSnapshotRepository(session)
    service = RuntimeSnapshotService(store, repo)
    return service, store, repo, session


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


async def test_apply_change_commits_snapshot_audit_outbox_and_finishes_pending(
    service_env,
) -> None:
    service, store, _repo, session = service_env

    def updater(draft):
        draft.basic_settings = []
        return "changed"

    new_state, result = await service.apply_change(session, _context(), updater)
    assert result == "changed"
    assert state_hash(store.load()) == state_hash(new_state)

    current = await service.get_current()
    assert current is not None and current.status == "active"

    mutation = (
        (await session.execute(select(RuntimeSnapshotMutation))).scalars().one()
    )
    assert mutation.status == "applied"
    assert mutation.target_current_digest == current.digest
    assert mutation.expected_admin_hash != mutation.target_admin_hash

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
    assert (await session.execute(select(RuntimeSnapshotMutation))).scalars().all() == []
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


async def test_apply_change_audit_failure_keeps_pending_and_rolls_back_snapshot(
    service_env, monkeypatch
) -> None:
    service, store, _repo, session = service_env
    expected_digest = projection_digest(build_runtime_projection(store.load()))

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

    # The pending row committed before the file rename must survive.
    mutation = (
        (await session.execute(select(RuntimeSnapshotMutation))).scalars().one()
    )
    assert mutation.status == "pending"
    # The snapshot side was rolled back (PG adapter participates in the
    # session transaction).
    snapshot_count = (
        await session.execute(text("SELECT count(*) FROM map_control.runtime_snapshots"))
    ).scalar_one()
    assert snapshot_count == 0
    # The file write already happened: reconciler can recover exactly.
    assert state_hash(store.load()) == mutation.target_admin_hash
    assert expected_digest != mutation.target_current_digest


async def test_reconciler_recovers_pending_mutations_exact_match(
    session, tmp_path
) -> None:
    state_file = str(tmp_path / "admin_state.json")
    store = AdminStateStore(state_file)
    expected_admin_hash = state_hash(store.load())

    # Build the target state/hash exactly like apply_change would.
    target_state = store.load().model_copy(deep=True)
    target_state.basic_settings = []
    target_admin_hash = state_hash(target_state)
    target_projection = build_runtime_projection(target_state)
    target_digest = projection_digest(target_projection)
    target_id = snapshot_id_for_digest(target_digest)

    # Case 1: NO_WRITE (file still at expected).
    async with async_sessionmaker(
        session.bind, class_=AsyncSession, expire_on_commit=False
    )() as s:
        s.add(
            RuntimeSnapshotMutation(
                resource="flow_policy:flow_policy",
                snapshot_id=target_id,
                expected_admin_hash=expected_admin_hash,
                target_admin_hash=target_admin_hash,
                expected_current_digest=None,
                target_current_digest=target_digest,
                target_projection=target_projection.model_dump(mode="json"),
                status="pending",
                action="update",
                actor_user_id="local-admin",
                actor_subject="admin",
                actor_roles=["platform_admin"],
                request_id="req-1",
            )
        )
        await s.commit()

    recovered = await reconcile_runtime_snapshot_mutations(
        async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False),
        store,
        PgRuntimeSnapshotRepository,
    )
    assert recovered == 1

    mutation = (
        (await session.execute(select(RuntimeSnapshotMutation))).scalars().one()
    )
    assert mutation.status == "failed"
    recovered_audit = (
        await session.execute(
            text(
                "SELECT status, failure_code, recovered FROM map_control.config_audit_events "
                "ORDER BY ordinal"
            )
        )
    ).all()
    assert len(recovered_audit) == 1
    assert recovered_audit[0].status == "failed"
    assert recovered_audit[0].failure_code == "NO_WRITE"
    assert recovered_audit[0].recovered is True
    assert await reconcile_runtime_snapshot_mutations(
        async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False),
        store,
        PgRuntimeSnapshotRepository,
    ) == 0


async def test_reconciler_applied_when_file_target_and_pointer_expected(
    session, tmp_path
) -> None:
    state_file = str(tmp_path / "admin_state.json")
    store = AdminStateStore(state_file)
    expected_admin_hash = state_hash(store.load())

    # Seed the current pointer with the OLD digest so the reconciler has to
    # insert the new snapshot and activate it (crash after file rename,
    # before snapshot insert).
    old_projection = _projection("old")
    old_digest = projection_digest(old_projection)
    old_id = snapshot_id_for_digest(old_digest)
    async with async_sessionmaker(
        session.bind, class_=AsyncSession, expire_on_commit=False
    )() as s:
        repo = PgRuntimeSnapshotRepository(s)
        await repo.insert(old_id, old_projection, old_digest, None, "published")
        await repo.activate(old_id, None)
        await s.commit()

    # Now write the target file directly (simulating the rename landing).
    target_state = store.load().model_copy(deep=True)
    target_state.basic_settings = []
    target_admin_hash = state_hash(target_state)
    store._write_atomic(target_state)
    target_projection = build_runtime_projection(target_state)
    target_digest = projection_digest(target_projection)
    target_id = snapshot_id_for_digest(target_digest)

    async with async_sessionmaker(
        session.bind, class_=AsyncSession, expire_on_commit=False
    )() as s:
        s.add(
            RuntimeSnapshotMutation(
                resource="flow_policy:flow_policy",
                snapshot_id=target_id,
                expected_admin_hash=expected_admin_hash,
                target_admin_hash=target_admin_hash,
                expected_current_digest=old_digest,
                target_current_digest=target_digest,
                target_projection=target_projection.model_dump(mode="json"),
                status="pending",
                action="update",
                actor_user_id="local-admin",
                actor_subject="admin",
                actor_roles=["platform_admin"],
                request_id="req-2",
            )
        )
        await s.commit()

    recovered = await reconcile_runtime_snapshot_mutations(
        async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False),
        store,
        PgRuntimeSnapshotRepository,
    )
    assert recovered == 1

    mutation = (
        (await session.execute(select(RuntimeSnapshotMutation))).scalars().one()
    )
    assert mutation.status == "applied"
    repo = PgRuntimeSnapshotRepository(session)
    current = await repo.get_current()
    assert current.id == target_id
    assert current.digest == target_digest
    assert current.parent_id == old_id
    old = await repo.get(old_id)
    assert old.status == "rolled_back"

    recovered_audit = (
        await session.execute(
            text(
                "SELECT status, failure_code, recovered, before_hash, after_hash "
                "FROM map_control.config_audit_events ORDER BY ordinal"
            )
        )
    ).all()
    assert len(recovered_audit) == 1
    assert recovered_audit[0].status == "applied"
    assert recovered_audit[0].failure_code is None
    assert recovered_audit[0].before_hash == old_digest
    assert recovered_audit[0].after_hash == target_digest


async def test_reconciler_unknown_state(session, tmp_path) -> None:
    state_file = str(tmp_path / "admin_state.json")
    store = AdminStateStore(state_file)

    mutation = RuntimeSnapshotMutation(
        resource="flow_policy:flow_policy",
        snapshot_id=uuid.uuid4(),
        expected_admin_hash="0" * 64,
        target_admin_hash="1" * 64,
        expected_current_digest=None,
        target_current_digest="2" * 64,
        target_projection=_projection("x").model_dump(mode="json"),
        status="pending",
        action="update",
        actor_user_id="local-admin",
        actor_subject="admin",
        actor_roles=["platform_admin"],
        request_id="req-3",
    )
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(mutation)
        await s.commit()

    recovered = await reconcile_runtime_snapshot_mutations(
        factory, store, PgRuntimeSnapshotRepository
    )
    assert recovered == 1
    row = (await session.execute(select(RuntimeSnapshotMutation))).scalars().one()
    assert row.status == "failed"
    failure = (
        await session.execute(
            text(
                "SELECT failure_code FROM map_control.config_audit_events "
                "WHERE recovered = true"
            )
        )
    ).scalars().one()
    assert failure == "UNKNOWN_STATE"


async def test_reconciler_bad_state_file(session, tmp_path) -> None:
    state_file = str(tmp_path / "admin_state.json")
    store = AdminStateStore(state_file)
    expected_admin_hash = state_hash(store.load())

    mutation = RuntimeSnapshotMutation(
        resource="flow_policy:flow_policy",
        snapshot_id=uuid.uuid4(),
        expected_admin_hash=expected_admin_hash,
        target_admin_hash="1" * 64,
        expected_current_digest=None,
        target_current_digest="2" * 64,
        target_projection=_projection("y").model_dump(mode="json"),
        status="pending",
        action="update",
        actor_user_id="local-admin",
        actor_subject="admin",
        actor_roles=["platform_admin"],
        request_id="req-4",
    )
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(mutation)
        await s.commit()

    import pathlib

    pathlib.Path(state_file).write_text("{not-json", encoding="utf-8")

    recovered = await reconcile_runtime_snapshot_mutations(
        factory, store, PgRuntimeSnapshotRepository
    )
    assert recovered == 1
    row = (await session.execute(select(RuntimeSnapshotMutation))).scalars().one()
    assert row.status == "failed"
    failure = (
        await session.execute(
            text(
                "SELECT failure_code FROM map_control.config_audit_events "
                "WHERE recovered = true"
            )
        )
    ).scalars().one()
    assert failure == "BAD_STATE_FILE"
