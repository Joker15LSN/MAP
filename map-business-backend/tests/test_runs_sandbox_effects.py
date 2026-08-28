"""Sandbox effect rules / projection / handler tests (Step 3 / PR-E).

Pure-rule tests use the real frozen state machine; handler tests cross the
RunWorker seam with the deterministic InMemorySandboxRemote adapter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.runs import (
    InMemoryCoreRunStream,
    InMemoryRunStore,
    InMemorySandboxRemote,
    RunApplication,
    RunWorker,
    build_create_key,
    build_execute_key,
    effect_executing,
    effect_failed,
    effect_planned,
    effect_succeeded,
    effect_uncertain,
    project_effects,
    request_digest,
)
from app.runs.domain import RunCommand
from app.runs.sandbox_remote import SandboxExecutionResult
from app.runtime.event_envelope import EventEnvelope
from app.runtime.state_machine import EffectState, StateTransitionError


def _sandbox_command(**payload_overrides) -> RunCommand:
    payload = {"command": "echo hi", "limits": {"timeout_seconds": 12}}
    payload.update(payload_overrides)
    return RunCommand(
        kind="sandbox_invocation",
        payload=payload,
        snapshot={"runtime": "v1"},
    )


def _workspace() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def store() -> InMemoryRunStore:
    return InMemoryRunStore(now=datetime(2026, 8, 24, tzinfo=UTC))


@pytest.fixture()
def application(store: InMemoryRunStore) -> RunApplication:
    return RunApplication(store)


async def _create_run(
    application: RunApplication, store: InMemoryRunStore, command: RunCommand
) -> uuid.UUID:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=command,
        idempotency_key=f"k-{uuid.uuid4().hex}",
        idempotency_body_hash=f"h-{uuid.uuid4().hex}",
    )
    assert store is not None
    return created.run_id


def _envelope(
    run_id: uuid.UUID, workspace_id: uuid.UUID, seq: int, event_type: str, data: dict
) -> EventEnvelope:
    return EventEnvelope.build(
        run_id=str(run_id),
        workspace_id=str(workspace_id),
        seq=seq,
        event_type=event_type,
        data=data,
    )


def test_request_digest_and_execution_keys_are_stable() -> None:
    digest = request_digest(
        command="echo hi", limits={"timeout_seconds": 30, "memory_mb": 512}
    )
    assert len(digest) == 64
    same = request_digest(
        command="echo hi", limits={"memory_mb": 512, "timeout_seconds": 30}
    )
    assert digest == same  # sort_keys=True: dict order must not matter
    assert build_create_key(
        workspace_id="ws-1", invocation_id="inv-1", request_digest=digest
    ) == f"create:ws-1:inv-1:{digest}"
    assert build_execute_key(
        workspace_id="ws-1", invocation_id="inv-1", request_digest=digest
    ) == f"execute:ws-1:inv-1:{digest}"


def test_effect_drafts_carry_the_full_durable_context() -> None:
    digest = request_digest(command="echo hi", limits={"timeout_seconds": 12})
    common = {
        "effect_id": "effect-1",
        "invocation_id": "inv-1",
        "command": "echo hi",
        "limits": {"timeout_seconds": 12},
        "request_digest": digest,
        "create_key": "create:ws-1:inv-1:digest",
        "execute_key": "execute:ws-1:inv-1:digest",
    }
    planned = effect_planned(**common)
    assert planned.type == "effect.planned"
    assert planned.data["status"] == EffectState.PLANNED
    executing = effect_executing(**common)
    assert executing.type == "effect.executing"
    assert executing.data["status"] == EffectState.EXECUTING
    succeeded = effect_succeeded(
        **common, sandbox_id="sb-1", output="ok"
    )
    assert succeeded.type == "effect.succeeded"
    assert succeeded.data["status"] == EffectState.SUCCEEDED
    assert succeeded.data["sandbox_id"] == "sb-1"
    failed = effect_failed(
        **common,
        error_code="OPENSANDBOX_FAILED",
        reason="remote returned an error",
    )
    assert failed.type == "effect.failed"
    assert failed.data["error_code"] == "OPENSANDBOX_FAILED"
    uncertain = effect_uncertain(
        **common,
        sandbox_id="sb-1",
        error_code="OPENSANDBOX_UNKNOWN_OUTCOME",
        reason="lost execute response",
    )
    assert uncertain.type == "effect.uncertain"
    assert uncertain.data["error_code"] == "OPENSANDBOX_UNKNOWN_OUTCOME"


def test_project_effects_folds_and_rejects_terminal_late_events() -> None:
    run_id = uuid.uuid4()
    ws = uuid.uuid4()
    common = {
        "effect_id": "effect-1",
        "invocation_id": "inv-1",
        "command": "echo hi",
        "limits": {"timeout_seconds": 12},
        "request_digest": "d" * 64,
        "create_key": "create:ws:inv:d",
        "execute_key": "execute:ws:inv:d",
    }
    events = [
        _envelope(run_id, ws, 1, "effect.planned", {**common}),
        _envelope(run_id, ws, 2, "effect.executing", {**common}),
        _envelope(
            run_id,
            ws,
            3,
            "effect.succeeded",
            {**common, "sandbox_id": "sb-1", "output": "ok"},
        ),
    ]
    views = project_effects(events)
    assert list(views) == ["effect-1"]
    view = views["effect-1"]
    assert view.status == EffectState.SUCCEEDED
    assert view.sandbox_id == "sb-1"
    assert view.execute_key == "execute:ws:inv:d"

    late = _envelope(
        run_id,
        ws,
        4,
        "effect.failed",
        {**common, "error_code": "OPENSANDBOX_FAILED", "reason": "late"},
    )
    with pytest.raises(StateTransitionError):
        project_effects([*events, late])


def test_project_effects_ignores_non_effect_events() -> None:
    run_id = uuid.uuid4()
    ws = uuid.uuid4()
    events = [
        _envelope(run_id, ws, 1, "run.started", {}),
        _envelope(
            run_id,
            ws,
            2,
            "effect.planned",
            {
                "effect_id": "effect-1",
                "invocation_id": "inv-1",
                "command": "echo hi",
                "limits": {"timeout_seconds": 12},
                "request_digest": "d" * 64,
                "create_key": "create:ws:inv:d",
                "execute_key": "execute:ws:inv:d",
            },
        ),
    ]
    views = project_effects(events)
    assert list(views) == ["effect-1"]
    assert views["effect-1"].status == EffectState.PLANNED


def test_in_memory_sandbox_remote_keyed_results_and_unknown() -> None:
    remote = InMemorySandboxRemote()
    remote.set_unknown("execute:ws:inv:d")
    from app.runs import SandboxExecutionRequest, SandboxIdentity

    request = SandboxExecutionRequest(
        identity=SandboxIdentity(
            workspace_id="ws", run_id="run", step_id="step",
            attempt_id="att", invocation_id="inv", client_request_id="req",
        ),
        command="echo hi",
        limits={},
        create_key="create:ws:inv:d",
        execute_key="execute:ws:inv:d",
    )

    async def run() -> None:
        first = await remote.execute(request)
        second = await remote.execute(request)
        assert first.status == "unknown"
        assert first.success is False
        assert second.status == "unknown"

    import asyncio

    asyncio.run(run())


async def test_sandbox_invocation_handler_success_event_order(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    command = _sandbox_command()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=command,
        idempotency_key="k-sandbox-ok",
        idempotency_body_hash="h-sandbox-ok",
    )
    remote = InMemorySandboxRemote(
        [
            SandboxExecutionResult(
                success=True,
                status="succeeded",
                sandbox_id="sb-1",
                output="ok",
                server_state={"status": "completed"},
            )
        ]
    )
    worker = RunWorker(
        store, InMemoryCoreRunStream([]), sandbox_remote=remote
    )
    outcome = await worker.run_once(worker_id="w-1")
    assert outcome is not None and outcome.run_status == "completed"

    events = [
        e
        async for e in application.replay_events(
            workspace_id=ws, principal_id="u-1", run_id=created.run_id
        )
    ]
    assert [e.type for e in events] == [
        "run.started",
        "attempt.started",
        "effect.planned",
        "effect.executing",
        "effect.succeeded",
        "attempt.completed",
        "run.completed",
    ]
    views = project_effects(events)
    assert views["effect-" + str(created.run_id) + "-1"].status == EffectState.SUCCEEDED
    assert views["effect-" + str(created.run_id) + "-1"].sandbox_id == "sb-1"
    assert remote.execute_calls[0].create_key.startswith("create:")
    assert remote.execute_calls[0].execute_key.startswith("execute:")


async def test_sandbox_invocation_handler_failed_settles_run_failed(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_sandbox_command(),
        idempotency_key="k-sandbox-fail",
        idempotency_body_hash="h-sandbox-fail",
    )
    remote = InMemorySandboxRemote(
        [
            SandboxExecutionResult(
                success=False,
                status="failed",
                error_code="OPENSANDBOX_FAILED",
                error_message="remote failed",
            )
        ]
    )
    outcome = await RunWorker(
        store, InMemoryCoreRunStream([]), sandbox_remote=remote
    ).run_once(worker_id="w-1")
    assert outcome is not None and outcome.run_status == "failed"

    events = [
        e
        async for e in application.replay_events(
            workspace_id=ws, principal_id="u-1", run_id=created.run_id
        )
    ]
    assert [e.type for e in events] == [
        "run.started",
        "attempt.started",
        "effect.planned",
        "effect.executing",
        "effect.failed",
        "attempt.failed",
        "run.failed",
    ]
    assert events[-1].data["code"] == "OPENSANDBOX_FAILED"


async def test_sandbox_invocation_handler_unknown_is_never_success(
    store: InMemoryRunStore, application: RunApplication
) -> None:
    ws = _workspace()
    created = await application.create_run(
        workspace_id=ws,
        principal_id="u-1",
        conversation_id=None,
        command=_sandbox_command(),
        idempotency_key="k-sandbox-unknown",
        idempotency_body_hash="h-sandbox-unknown",
    )
    remote = InMemorySandboxRemote(
        [
            SandboxExecutionResult(
                success=False,
                status="unknown",
                sandbox_id="sb-1",
                error_code="OPENSANDBOX_UNKNOWN_OUTCOME",
                error_message="lost response",
            )
        ]
    )
    outcome = await RunWorker(
        store, InMemoryCoreRunStream([]), sandbox_remote=remote
    ).run_once(worker_id="w-1")
    assert outcome is not None and outcome.run_status == "failed"

    events = [
        e
        async for e in application.replay_events(
            workspace_id=ws, principal_id="u-1", run_id=created.run_id
        )
    ]
    assert [e.type for e in events] == [
        "run.started",
        "attempt.started",
        "effect.planned",
        "effect.executing",
        "effect.uncertain",
        "attempt.failed",
        "run.failed",
    ]
    view = await application.get_run(
        workspace_id=ws, principal_id="u-1", run_id=created.run_id
    )
    assert view.status == "failed"
    assert remote.execute_calls[0].execute_key.startswith("execute:")
