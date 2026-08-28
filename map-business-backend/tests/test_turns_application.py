"""Turn application tests (Step 4 / PR-F1+F2).

No in-memory TurnStore is implemented for this step, so the application
tests run against the real PostgreSQL fixtures (the same three-role setup
as ``tests/integration``). Pure projection rules are covered without a DB.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Conversation, Message
from app.runs import InMemoryCoreRunStream, PgRunStore, RunApplication, RunWorker
from app.runs.domain import CoreOutcome
from app.runs.errors import RunNotFoundError
from app.runtime.event_envelope import EventEnvelope
from app.runtime.state_machine import RunState
from app.turns import TurnApplication, TurnError
from app.turns.projection import project_turn_events

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _envelope(seq: int, event_type: str, data: dict | None = None) -> EventEnvelope:
    return EventEnvelope.build(
        run_id=str(RUN_ID),
        seq=seq,
        event_type=event_type,
        workspace_id=str(WORKSPACE),
        data=data or {},
    )


# --- pure projection rules ----------------------------------------------------


def test_project_turn_events_dedupes_and_renders_terminal_once() -> None:
    events = [
        _envelope(1, "run.started"),
        _envelope(2, "message.delta", {"content": "你"}),
        _envelope(2, "message.delta", {"content": "重复"}),
        _envelope(3, "message.delta", {"content": "好"}),
        _envelope(4, "step.completed", {"content": "你好啊"}),
        _envelope(5, "run.completed"),
        _envelope(6, "run.failed", {"code": "LATE"}),
    ]
    folded = project_turn_events(events)
    assert folded.run_id == str(RUN_ID)
    assert folded.content == "你好啊"
    assert folded.terminal_status == RunState.COMPLETED
    assert folded.terminal_seen is True
    assert folded.last_seq == 6


def test_project_turn_events_message_delta_without_step_completed() -> None:
    events = [
        _envelope(1, "run.started"),
        _envelope(2, "message.delta", {"content": "你"}),
        _envelope(3, "message.delta", {"content": "好"}),
        _envelope(4, "run.completed"),
    ]
    folded = project_turn_events(events)
    assert folded.content == "你好"
    assert folded.terminal_status == RunState.COMPLETED


# --- application over PostgreSQL ---------------------------------------------


@pytest.fixture()
def factory(_engine):
    return async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture()
def run_application(factory) -> RunApplication:
    return RunApplication(PgRunStore(factory))


@pytest.fixture()
def turn_application(factory, run_application) -> TurnApplication:
    return TurnApplication(factory, run_application)


async def _conversation(session) -> Conversation:
    conversation = Conversation(
        workspace_id=WORKSPACE,
        owner_user_id="u-1",
        mode="global",
        title="app-test",
    )
    session.add(conversation)
    await session.commit()
    return conversation


@pytest.mark.asyncio
async def test_start_turn_replay_returns_stored_triple(
    turn_application: TurnApplication, session
) -> None:
    conversation = await _conversation(session)
    first = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-app",
        idempotency_key="app-k-1",
        idempotency_body_hash="hash-a",
    )
    replay = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-app",
        idempotency_key="app-k-1",
        idempotency_body_hash="hash-a",
    )
    assert replay.replayed is True
    assert (replay.run_id, replay.user_message_id, replay.assistant_message_id) == (
        first.run_id,
        first.user_message_id,
        first.assistant_message_id,
    )
    with pytest.raises(TurnError) as conflict:
        await turn_application.start_turn(
            workspace_id=WORKSPACE,
            principal_id="u-1",
            conversation_id=conversation.id,
            query="hello",
            request_id="req-app",
            idempotency_key="app-k-1",
            idempotency_body_hash="hash-b",
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_stop_turn_after_run_terminal_does_not_finalize_message(
    turn_application: TurnApplication, factory, session
) -> None:
    conversation = await _conversation(session)
    created = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-terminal",
        idempotency_key="app-k-2",
        idempotency_body_hash="hash-c",
    )
    worker = RunWorker(
        PgRunStore(factory),
        InMemoryCoreRunStream([CoreOutcome(status="completed")]),
    )
    outcome = await worker.run_once(worker_id="app-worker")
    assert outcome is not None and outcome.run_status == RunState.COMPLETED

    receipt = await turn_application.stop_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        message_id=created.assistant_message_id,
    )
    assert receipt is not None
    assert receipt.accepted is False
    assert receipt.run_status == RunState.COMPLETED

    from sqlalchemy import select

    message = (
        await session.execute(
            select(Message)
            .where(Message.id == created.assistant_message_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert message.status == "streaming"


@pytest.mark.asyncio
async def test_get_turn_projection_missing_run_is_run_not_found(
    turn_application: TurnApplication,
) -> None:
    with pytest.raises(RunNotFoundError):
        await turn_application.get_turn_projection(
            workspace_id=WORKSPACE,
            principal_id="u-1",
            run_id=uuid.uuid4(),
        )
