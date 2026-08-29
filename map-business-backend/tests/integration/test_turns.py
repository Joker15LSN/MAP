"""Step 4 / PR-F1+F2: turn transaction, stop adapter, projection recovery.

Real PostgreSQL tests for the Turn application over the shared
``_engine``/``session`` fixtures. The application is exercised through the
same seam as the route, plus the public HTTP path for the new endpoint.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_turns_test_state.json")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_run_application, get_turn_application
from app.core.identity import AuthMode
from app.db.models import Conversation, IdempotencyRecord, Job, Message, Run
from app.main import create_app
from app.runs import (
    InMemoryCoreRunStream,
    PgRunStore,
    RunApplication,
    RunWorker,
)
from app.runs.domain import CoreEvent, CoreOutcome
from app.runtime.state_machine import RunState
from app.schemas import AdminState
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.schemas import build_runtime_projection
from app.settings import Settings
from app.turns import (
    StopTurnReceipt,
    TurnApplication,
    TurnCreated,
    TurnError,
    TurnNotFoundError,
)
from app.turns.projection import project_turn_events

pytestmark = pytest.mark.asyncio

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture()
def factory(_engine):
    return async_sessionmaker(_engine, expire_on_commit=False)


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
def run_application(factory) -> RunApplication:
    return RunApplication(PgRunStore(factory))


@pytest.fixture()
def turn_application(factory, run_application, current_snapshot) -> TurnApplication:
    del current_snapshot
    return TurnApplication(factory, run_application)


@pytest.fixture()
async def http_client(
    _engine, session, run_application, turn_application
) -> AsyncClient:
    del session  # test isolation side effect is enough
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file="/tmp/map_bff_turns_test_state.json",
            default_workspace_id=str(WORKSPACE),
        )
    )
    app.dependency_overrides[get_run_application] = lambda: run_application
    app.dependency_overrides[get_turn_application] = lambda: turn_application
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _create_conversation(
    session, workspace_id: uuid.UUID = WORKSPACE, owner_user_id: str = "u-1"
) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        mode="global",
        title="turn-test",
    )
    session.add(conversation)
    await session.commit()
    return conversation


def _body(query: str = "hello", request_id: str = "req-1") -> dict:
    return {"query": query, "request_id": request_id}


async def test_start_turn_is_one_transaction_and_idempotent(
    turn_application: TurnApplication, session
) -> None:
    conversation = await _create_conversation(session)
    first = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-1",
        idempotency_key="turn-k-1",
        idempotency_body_hash="hash-1",
    )
    assert first.replayed is False
    assert first.status == RunState.QUEUED

    replay = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-1",
        idempotency_key="turn-k-1",
        idempotency_body_hash="hash-1",
    )
    assert replay.replayed is True
    assert replay.run_id == first.run_id
    assert replay.user_message_id == first.user_message_id
    assert replay.assistant_message_id == first.assistant_message_id

    run = (
        await session.execute(
            select(Run).where(Run.id == first.run_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    job = (
        await session.execute(
            select(Job).where(Job.id == first.run_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    messages = (
        (
            await session.execute(
                select(Message)
                .where(Message.run_id == first.run_id)
                .order_by(Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    idempotency = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.workspace_id == WORKSPACE,
                IdempotencyRecord.principal_id == "u-1",
                IdempotencyRecord.key == "turn-k-1",
            )
        )
    ).scalar_one()
    assert run is not None and run.status == RunState.QUEUED
    assert run.conversation_id == conversation.id
    assert job is not None and job.job_type == "run"
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "hello"
    assert messages[1].status == "streaming"
    assert idempotency.request_hash == "hash-1"
    reloaded = (
        await session.execute(
            select(Conversation)
            .where(Conversation.id == conversation.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reloaded.last_message_at is not None


async def test_start_turn_conflict_and_ownership(
    turn_application: TurnApplication, session
) -> None:
    conversation = await _create_conversation(session)
    await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-1",
        idempotency_key="turn-k-2",
        idempotency_body_hash="hash-2",
    )
    with pytest.raises(TurnError) as conflict:
        await turn_application.start_turn(
            workspace_id=WORKSPACE,
            principal_id="u-1",
            conversation_id=conversation.id,
            query="CHANGED",
            request_id="req-1",
            idempotency_key="turn-k-2",
            idempotency_body_hash="hash-other",
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

    with pytest.raises(TurnNotFoundError):
        await turn_application.start_turn(
            workspace_id=uuid.uuid4(),
            principal_id="u-1",
            conversation_id=conversation.id,
            query="hello",
            request_id="req-2",
            idempotency_key="turn-k-3",
            idempotency_body_hash="hash-3",
        )
    with pytest.raises(TurnNotFoundError):
        await turn_application.start_turn(
            workspace_id=WORKSPACE,
            principal_id="u-other",
            conversation_id=conversation.id,
            query="hello",
            request_id="req-2",
            idempotency_key="turn-k-3",
            idempotency_body_hash="hash-3",
        )


async def test_http_start_turn_and_replay_envelopes(
    http_client: AsyncClient, session
) -> None:
    # dev auth principal is "local-admin"; the route must see an owner match.
    conversation = await _create_conversation(session, owner_user_id="local-admin")
    response = await http_client.post(
        f"/api/v1/conversations/{conversation.id}/turns",
        json=_body(),
        headers={"Idempotency-Key": "turn-route-1"},
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "run_id",
        "user_message_id",
        "assistant_message_id",
        "status",
        "replayed",
    }
    assert body["replayed"] is False

    replay = await http_client.post(
        f"/api/v1/conversations/{conversation.id}/turns",
        json=_body(),
        headers={"Idempotency-Key": "turn-route-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["run_id"] == body["run_id"]
    assert replay.json()["replayed"] is True

    conflict = await http_client.post(
        f"/api/v1/conversations/{conversation.id}/turns",
        json=_body(query="changed"),
        headers={"Idempotency-Key": "turn-route-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    missing = await http_client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/turns",
        json=_body(),
        headers={"Idempotency-Key": "turn-route-2"},
    )
    assert missing.status_code == 404


async def test_http_start_turn_without_current_snapshot_is_503(
    http_client: AsyncClient, session
) -> None:
    await session.execute(
        text("DELETE FROM map_control.runtime_snapshot_current WHERE id = 1")
    )
    await session.commit()
    conversation = await _create_conversation(session, owner_user_id="local-admin")
    response = await http_client.post(
        f"/api/v1/conversations/{conversation.id}/turns",
        json=_body(),
        headers={"Idempotency-Key": "turn-route-no-snapshot"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "RUNTIME_SNAPSHOT_UNAVAILABLE"


async def test_stop_turn_thin_adapter_cancels_run_and_finalizes_message(
    turn_application: TurnApplication, session
) -> None:
    conversation = await _create_conversation(session)
    created: TurnCreated = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-stop",
        idempotency_key="turn-stop-1",
        idempotency_body_hash="hash-stop",
    )
    receipt = await turn_application.stop_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        message_id=created.assistant_message_id,
    )
    assert isinstance(receipt, StopTurnReceipt)
    assert receipt.accepted is True
    assert receipt.run_id == created.run_id
    assert receipt.run_status == RunState.QUEUED

    run = await session.get(Run, created.run_id)
    assert run is not None and run.cancel_requested_at is not None
    message = await session.get(Message, created.assistant_message_id)
    assert message is not None and message.status == "stopped"

    # A second stop is accepted=False (the command is already recorded).
    duplicate = await turn_application.stop_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        message_id=created.assistant_message_id,
    )
    assert duplicate is not None
    assert duplicate.accepted is False


async def test_stop_turn_legacy_message_without_run(
    turn_application: TurnApplication, session
) -> None:
    conversation = await _create_conversation(session)
    user = Message(
        conversation_id=conversation.id,
        workspace_id=WORKSPACE,
        role="user",
        status="completed",
        content="legacy",
        request_id="legacy-req",
        completed_at=datetime.now(UTC),
    )
    assistant = Message(
        conversation_id=conversation.id,
        workspace_id=WORKSPACE,
        role="assistant",
        status="streaming",
        content="",
        request_id="legacy-req",
    )
    session.add_all([user, assistant])
    await session.commit()

    receipt = await turn_application.stop_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        message_id=assistant.id,
    )
    assert receipt is not None
    assert receipt.run_id is None
    assert receipt.accepted is False
    message = (
        await session.execute(
            select(Message)
            .where(Message.id == assistant.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert message.status == "stopped"


async def test_get_turn_projection_recovers_from_run_events(
    turn_application: TurnApplication,
    run_application: RunApplication,
    factory,
    session,
) -> None:
    conversation = await _create_conversation(session)
    created = await turn_application.start_turn(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        conversation_id=conversation.id,
        query="hello",
        request_id="req-proj",
        idempotency_key="turn-proj-1",
        idempotency_body_hash="hash-proj",
    )
    worker = RunWorker(
        PgRunStore(factory),
        InMemoryCoreRunStream(
            [
                CoreEvent(type="message.delta", data={"content": "你"}),
                CoreEvent(type="message.delta", data={"content": "好"}),
                CoreEvent(
                    type="step.completed",
                    data={"content": "你好啊", "result": {}},
                ),
                CoreOutcome(status="completed"),
            ]
        ),
    )
    outcome = await worker.run_once(worker_id="turn-worker")
    assert outcome is not None and outcome.run_status == RunState.COMPLETED

    projection = await turn_application.get_turn_projection(
        workspace_id=WORKSPACE,
        principal_id="u-1",
        run_id=created.run_id,
    )
    assert projection is not None
    assert projection.run_id == created.run_id
    assert projection.content == "你好啊"  # step.completed full text wins
    assert projection.terminal_seen is True
    assert projection.status == RunState.COMPLETED
    assert projection.user_message_id == created.user_message_id
    assert projection.assistant_message_id == created.assistant_message_id

    # Pure projection also drops late terminal events (stop/done race).
    events = [
        envelope
        async for envelope in run_application.replay_events(
            workspace_id=WORKSPACE,
            principal_id="u-1",
            run_id=created.run_id,
        )
    ]
    folded = project_turn_events(events)
    assert folded.terminal_status == RunState.COMPLETED
