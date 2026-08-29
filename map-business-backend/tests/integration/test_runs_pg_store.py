"""Real PostgreSQL tests for the Canonical Run internal seam.

The in-memory adapter models the semantics; these tests prove the SQL facts
the contract depends on: one-transaction create, SKIP LOCKED single winner,
lease fencing, sequence uniqueness and terminal CAS. Run with the release
gate's three-role PostgreSQL (the shared conftest applies migrations).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import IdempotencyRecord, Job, JobStatus, Run
from app.repositories.jobs import JobRepository
from app.runs import PgRunStore
from app.runs.domain import RunCommand, RunEventDraft
from app.runs.errors import (
    IdempotencyConflictRunError,
    LeaseLostError,
    RunStateTransitionError,
)
from app.schemas import AdminState
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.schemas import build_runtime_projection

T0 = datetime(2026, 8, 24, tzinfo=UTC)


def _command() -> RunCommand:
    return RunCommand(
        kind="conversation_turn",
        payload={"query": "hello"},
        snapshot={"runtime": "v1"},
    )


@pytest.fixture()
async def current_snapshot(session) -> tuple[uuid.UUID, str]:
    projection = build_runtime_projection(AdminState.default())
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    repo = PgRuntimeSnapshotRepository(session)
    await repo.insert(snapshot_id, projection, digest, None, "published")
    await repo.activate(snapshot_id, None)
    await session.commit()
    return snapshot_id, digest


@pytest.fixture()
def store(_engine, session, current_snapshot) -> PgRunStore:
    # ``session`` is requested only for its test-isolation side effect: the
    # shared conftest fixture truncates map_control tables with the admin
    # role before each test, so runs/jobs never leak across tests.
    # ``current_snapshot`` guarantees every run creation below has a
    # current pointer to pin.
    del session, current_snapshot
    factory = async_sessionmaker(_engine, expire_on_commit=False)
    return PgRunStore(factory)


async def _current_snapshot_args(
    store: PgRunStore,
) -> tuple[uuid.UUID, str]:
    async with store._session_factory() as session:
        current = await PgRuntimeSnapshotRepository(session).get_current()
        assert current is not None
        return current.id, current.digest


async def _create(store: PgRunStore, ws: uuid.UUID, key: str = "k-1"):
    snapshot_id, snapshot_digest = await _current_snapshot_args(store)
    return await store.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        runtime_snapshot_id=snapshot_id,
        runtime_snapshot_digest=snapshot_digest,
        idempotency_key=key,
        idempotency_body_hash=f"hash-{key}",
        now=T0,
    )


async def test_create_is_one_transaction_and_replays(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    result = await _create(store, ws)
    replay = await _create(store, ws)
    assert replay.created.replayed is True
    assert replay.created.run_id == result.created.run_id

    async with store._session_factory() as session:
        run = await session.get(Run, result.created.run_id)
        job = await session.get(Job, result.created.run_id)
        idem = (
            await session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.workspace_id == ws,
                    IdempotencyRecord.key == "k-1",
                )
            )
        ).scalar_one()
        assert run is not None and job is not None
        assert job.job_type == "run"
        assert job.status == JobStatus.QUEUED
        assert idem.request_hash == "hash-k-1"


async def test_create_conflict_is_typed(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    await _create(store, ws, key="k-1")
    snapshot_id, snapshot_digest = await _current_snapshot_args(store)
    with pytest.raises(IdempotencyConflictRunError):
        await store.create_run(
            workspace_id=ws,
            principal_id="u-1",
            conversation_id=None,
            command=_command(),
            runtime_snapshot_id=snapshot_id,
            runtime_snapshot_digest=snapshot_digest,
            idempotency_key="k-1",
            idempotency_body_hash="hash-other",
            now=T0,
        )


async def test_claim_skip_locked_has_single_winner(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    await _create(store, ws)
    first, second = await asyncio.gather(
        store.claim_next(worker_id="w-1", lease_seconds=10, now=T0),
        store.claim_next(worker_id="w-2", lease_seconds=10, now=T0),
    )
    claims = [c for c in (first, second) if c is not None]
    assert len(claims) == 1
    assert claims[0].attempt == 1


async def test_lease_fencing_and_takeover(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    await _create(store, ws)
    claim = await store.claim_next(worker_id="w-1", lease_seconds=1, now=T0)
    assert claim is not None

    envelopes = await store.append_events(
        claim=claim,
        drafts=[RunEventDraft(type="attempt.started", data={})],
        now=T0,
    )
    assert [e.seq for e in envelopes] == [1]

    with pytest.raises(LeaseLostError):
        await store.append_events(
            claim=claim,
            drafts=[RunEventDraft(type="step.started", data={})],
            now=T0 + timedelta(seconds=2),
        )

    takeover = await store.claim_next(
        worker_id="w-2", lease_seconds=10, now=T0 + timedelta(seconds=2)
    )
    assert takeover is not None
    assert takeover.attempt == claim.attempt + 1


async def test_terminal_cas_single_winner(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    await _create(store, ws)
    claim = await store.claim_next(worker_id="w-1", lease_seconds=60, now=T0)
    assert claim is not None

    started = await store.settle_terminal(
        claim=claim, event_type="run.started", data={}, now=T0
    )
    assert started.seq == 1
    await store.append_events(
        claim=claim,
        drafts=[
            RunEventDraft(type="attempt.completed", data={"attempt": claim.attempt})
        ],
        now=T0,
    )
    await store.settle_terminal(
        claim=claim, event_type="run.completed", data={}, now=T0
    )
    with pytest.raises(RunStateTransitionError):
        await store.settle_terminal(
            claim=claim, event_type="run.failed", data={}, now=T0
        )


async def test_events_replay_in_seq_order(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    created = await _create(store, ws)
    claim = await store.claim_next(worker_id="w-1", lease_seconds=60, now=T0)
    assert claim is not None
    await store.settle_terminal(claim=claim, event_type="run.started", data={}, now=T0)
    await store.append_events(
        claim=claim,
        drafts=[
            RunEventDraft(type="step.started", data={"step_id": "s-1"}),
            RunEventDraft(type="step.completed", data={"step_id": "s-1"}),
        ],
        now=T0,
    )
    await store.settle_terminal(claim=claim, event_type="run.completed", data={}, now=T0)
    events = [
        e
        async for e in store.read_events_after(
            workspace_id=ws,
            principal_id="u-1",
            run_id=created.created.run_id,
            after_seq=0,
        )
    ]
    assert [e.type for e in events] == [
        "run.started",
        "step.started",
        "step.completed",
        "run.completed",
    ]
    assert [e.seq for e in events] == [1, 2, 3, 4]


async def test_run_job_parked_until_run_worker_claims(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    created = await _create(store, ws)
    # The legacy JobRunner claims ONLY registered handler types; run jobs
    # must wait for the RunWorker loop (PR-D), never fail as unknown type.
    factory = store._session_factory
    async with factory() as session:
        repo = JobRepository(session)
        legacy = await repo.claim_next(
            worker_id="legacy-w",
            lease_seconds=60,
            job_types=["message_reconcile", "sandbox_exec"],
        )
        assert legacy is None

    run_claim = await store.claim_next(worker_id="run-w", lease_seconds=60, now=T0)
    assert run_claim is not None
    assert run_claim.run_id == created.created.run_id


async def test_retry_schedules_job_and_terminal_settles(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    await _create(store, ws)
    claim = await store.claim_next(worker_id="w-1", lease_seconds=60, now=T0)
    assert claim is not None
    await store.settle_terminal(claim=claim, event_type="run.started", data={}, now=T0)

    scheduled = await store.fail_attempt(
        claim=claim,
        error_code="HANDLER_ERROR",
        error_message="boom",
        retryable=True,
        now=T0,
    )
    assert scheduled is True

    async with store._session_factory() as session:
        job = await session.get(Job, claim.run_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED
        assert job.next_run_at is not None and job.next_run_at > T0

    retry_at = T0 + timedelta(seconds=2)
    second = await store.claim_next(
        worker_id="w-1", lease_seconds=60, now=retry_at
    )
    assert second is not None and second.attempt == 2


async def test_retry_exhaustion_settles_run_failed(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    created = await _create(store, ws)
    claim = await store.claim_next(worker_id="w-1", lease_seconds=60, now=T0)
    assert claim is not None
    await store.settle_terminal(claim=claim, event_type="run.started", data={}, now=T0)

    now = T0
    for attempt_no in (1, 2, 3):
        scheduled = await store.fail_attempt(
            claim=claim,
            error_code="HANDLER_ERROR",
            error_message="boom",
            retryable=True,
            now=now,
        )
        if attempt_no < 3:
            assert scheduled is True
            now = now + timedelta(seconds=2 ** attempt_no + 1)
            claim = await store.claim_next(
                worker_id="w-1", lease_seconds=60, now=now
            )
            assert claim is not None and claim.attempt == attempt_no + 1
        else:
            assert scheduled is False

    await store.settle_terminal(
        claim=claim,
        event_type="run.failed",
        data={"code": "HANDLER_ERROR", "message": "boom"},
        now=now,
    )
    view = await store.get_run_view(
        workspace_id=ws, principal_id="u-1", run_id=created.created.run_id
    )
    assert view is not None and view.status == "failed"
    async with store._session_factory() as session:
        job = await session.get(Job, claim.run_id)
        assert job is not None and job.status == JobStatus.FAILED


async def test_cancel_command_never_changes_run_status(store: PgRunStore) -> None:
    ws = uuid.uuid4()
    created = await _create(store, ws)
    receipt = await store.submit_cancel_command(
        workspace_id=ws,
        principal_id="u-1",
        run_id=created.created.run_id,
        reason="stop",
        now=T0,
    )
    assert receipt is not None and receipt.accepted is True
    view = await store.get_run_view(
        workspace_id=ws, principal_id="u-1", run_id=created.created.run_id
    )
    assert view is not None
    assert view.status == "queued"
    assert view.cancel_requested is True
