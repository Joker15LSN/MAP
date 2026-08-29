"""Interface tests for the Canonical Run module (Step 2 / PR-C).

These tests cross the same seam every caller crosses: RunApplication for the
BFF and RunWorker for the worker. The in-memory RunStore is the second
adapter of the internal seam; its CAS/SKIP LOCKED semantics are verified
against the real PostgreSQL adapter in
``tests/integration/test_runs_pg_store.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.runs import (
    InMemoryCoreRunStream,
    InMemoryRunStore,
    RunApplication,
    RunWorker,
)
from app.runs.attempt import AttemptAborted
from app.runs.domain import (
    CoreError,
    CoreEvent,
    CoreItem,
    CoreOutcome,
    RunCommand,
)
from app.runs.errors import (
    IdempotencyConflictRunError,
    RunNotFoundError,
    RunTerminalStateError,
)


def _command() -> RunCommand:
    return RunCommand(
        kind="conversation_turn",
        payload={"query": "hello"},
        snapshot={"runtime": "v1"},
    )


def _workspace() -> uuid.UUID:
    return uuid.uuid4()


_RUNTIME_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_RUNTIME_SNAPSHOT_DIGEST = "a" * 64


@pytest.fixture()
def store() -> InMemoryRunStore:
    return InMemoryRunStore(now=datetime(2026, 8, 24, tzinfo=UTC))


@pytest.fixture()
def application(store: InMemoryRunStore) -> RunApplication:
    return RunApplication(store)


async def test_create_run_and_replay_idempotency(
    application: RunApplication, store: InMemoryRunStore
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    assert created.status == "queued"
    assert created.replayed is False

    replay = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    assert replay.run_id == created.run_id
    assert replay.replayed is True


async def test_create_run_idempotency_conflict(application: RunApplication) -> None:
    ws = _workspace()
    await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    with pytest.raises(IdempotencyConflictRunError):
        await application.create_run(
            workspace_id=ws,
            principal_id="u-1",
            conversation_id=None,
            command=_command(),
            idempotency_key="k-1",
            idempotency_body_hash="hash-b",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
        )


async def test_get_run_cross_workspace_is_not_found(
    application: RunApplication,
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    view = await application.get_run(workspace_id=ws, principal_id="u-1", run_id=created.run_id)
    assert view.run_id == created.run_id
    assert view.runtime_snapshot_id == _RUNTIME_SNAPSHOT_ID
    assert view.runtime_snapshot_digest == _RUNTIME_SNAPSHOT_DIGEST
    with pytest.raises(RunNotFoundError):
        await application.get_run(
            workspace_id=_workspace(), principal_id="u-1", run_id=created.run_id
        )
    with pytest.raises(RunNotFoundError):
        await application.get_run(
            workspace_id=ws, principal_id="u-other", run_id=created.run_id
        )


async def test_cancel_is_command_only_until_worker_settles(
    application: RunApplication, store: InMemoryRunStore
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    receipt = await application.cancel_run(
        workspace_id=ws, run_id=created.run_id, principal_id="u-1", reason="stop"
    )
    assert receipt.accepted is True
    view = await application.get_run(workspace_id=ws, principal_id="u-1", run_id=created.run_id)
    # BFF wrote the command fact only; the worker owns the transition.
    assert view.status == "queued"
    assert view.cancel_requested is True

    duplicate = await application.cancel_run(
        workspace_id=ws, run_id=created.run_id, principal_id="u-1", reason="stop"
    )
    assert duplicate.accepted is False
    assert duplicate.status == "queued"


async def test_worker_happy_path_event_order(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    core = InMemoryCoreRunStream(
        [
            CoreEvent(type="step.started", data={"step_id": "s-1"}),
            CoreEvent(type="step.completed", data={"step_id": "s-1"}),
            CoreOutcome(status="completed"),
        ]
    )
    worker = RunWorker(store, core)
    outcome = await worker.run_once(worker_id="w-1")
    assert outcome is not None
    assert outcome.run_status == "completed"

    events = [
        envelope
        async for envelope in application.replay_events(
            workspace_id=ws, principal_id="u-1", run_id=created.run_id
        )
    ]
    assert [e.type for e in events] == [
        "run.started",
        "attempt.started",
        "step.started",
        "step.completed",
        "attempt.completed",
        "run.completed",
    ]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]

    view = await application.get_run(workspace_id=ws, principal_id="u-1", run_id=created.run_id)
    assert view.status == "completed"
    with pytest.raises(RunTerminalStateError):
        await application.cancel_run(
            workspace_id=ws, run_id=created.run_id, principal_id="u-1"
        )


async def test_worker_core_failure_settles_run_failed(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    core = InMemoryCoreRunStream(
        [CoreOutcome(status="failed", error_code="CORE_BAD", error_message="bad")]
    )
    outcome = await RunWorker(store, core).run_once(worker_id="w-1")
    assert outcome is not None and outcome.run_status == "failed"

    events = [
        e
        async for e in application.replay_events(
            workspace_id=ws, principal_id="u-1", run_id=created.run_id
        )
    ]
    assert events[-1].type == "run.failed"
    assert events[-1].data["code"] == "CORE_BAD"
    view = await application.get_run(workspace_id=ws, principal_id="u-1", run_id=created.run_id)
    assert view.status == "failed"


async def test_worker_core_transport_error_retries_then_fails(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    core = InMemoryCoreRunStream(
        [CoreError(code="STREAM_CORE_ERROR", message="upstream broke")]
    )
    worker = RunWorker(store, core)
    first = await worker.run_once(worker_id="w-1")
    assert first is not None
    assert first.run_status == "running"
    assert first.attempt_retryable is True

    store.set_clock(store.now + timedelta(seconds=3))
    second = await worker.run_once(worker_id="w-1")
    assert second is not None and second.attempt_retryable is True

    store.set_clock(store.now + timedelta(seconds=5))
    third = await worker.run_once(worker_id="w-1")
    assert third is not None
    assert third.run_status == "failed"
    assert third.attempt_retryable is False
    view = await application.get_run(
        workspace_id=ws, principal_id="u-1", run_id=created.run_id
    )
    assert view.status == "failed"


async def test_worker_handler_exception_retries_within_max_attempts(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )

    calls = 0

    async def flaky(attempt) -> AsyncIterator[CoreItem]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("flaky handler")
        yield CoreOutcome(status="completed")

    worker = RunWorker(store, InMemoryCoreRunStream([]), handler=flaky)
    first = await worker.run_once(worker_id="w-1")
    assert first is not None and first.attempt_retryable is True
    store.set_clock(store.now + timedelta(seconds=3))
    second = await worker.run_once(worker_id="w-1")
    assert second is not None and second.attempt_retryable is True
    store.set_clock(store.now + timedelta(seconds=5))
    third = await worker.run_once(worker_id="w-1")
    assert third is not None and third.run_status == "completed"

    view = await application.get_run(
        workspace_id=ws, principal_id="u-1", run_id=created.run_id
    )
    assert view.status == "completed"


async def test_worker_cancel_command_settles_cancelled(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    await application.cancel_run(
        workspace_id=ws, run_id=created.run_id, principal_id="u-1"
    )
    core = InMemoryCoreRunStream([CoreOutcome(status="completed")])
    outcome = await RunWorker(store, core).run_once(worker_id="w-1")
    assert outcome is not None and outcome.run_status == "cancelled"
    events = [
        e
        async for e in application.replay_events(
            workspace_id=ws, principal_id="u-1", run_id=created.run_id
        )
    ]
    assert [e.type for e in events] == ["run.cancelling", "run.cancelled"]


async def test_worker_lease_takeover_fences_loser(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    first = await store.claim_next(worker_id="w-1", lease_seconds=10)
    assert first is not None
    store.set_clock(store.now + timedelta(seconds=11))
    second = await store.claim_next(worker_id="w-2", lease_seconds=10)
    assert second is not None
    assert second.attempt == first.attempt + 1

    from app.runs.errors import LeaseLostError

    with pytest.raises(LeaseLostError):
        await store.settle_terminal(claim=first, event_type="run.started", data={})


async def test_replay_after_seq_is_strictly_increasing(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    core = InMemoryCoreRunStream([CoreOutcome(status="completed")])
    await RunWorker(store, core).run_once(worker_id="w-1")
    resumed = [
        e
        async for e in application.replay_events(
            workspace_id=ws,
            principal_id="u-1",
            run_id=created.run_id,
            after_seq=3,
        )
    ]
    assert [e.seq for e in resumed] == [4]


async def test_handler_generator_is_aclosed_on_stop(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_command(),
        idempotency_key="k-1",
        idempotency_body_hash="hash-a",
        runtime_snapshot_id=_RUNTIME_SNAPSHOT_ID,
        runtime_snapshot_digest=_RUNTIME_SNAPSHOT_DIGEST,
    )
    closed = asyncio.Event()

    async def slow_handler(attempt) -> AsyncIterator[CoreOutcome | CoreEvent]:
        try:
            yield CoreEvent(type="step.started", data={})
            await asyncio.Event().wait()  # blocks forever until aclose
        finally:
            closed.set()

    stop = asyncio.Event()
    task = asyncio.create_task(
        RunWorker(store, InMemoryCoreRunStream([]), handler=slow_handler).run_once(
            worker_id="w-1", stop_event=stop
        )
    )
    await asyncio.sleep(0.05)
    stop.set()
    with pytest.raises(AttemptAborted):
        await task
    assert closed.is_set()
