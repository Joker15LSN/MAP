"""BFF-facing Turn application (Step 4 / PR-F1+F2).

A turn is the user-visible unit that starts one canonical Run and writes
the two durable Message facts in the SAME transaction as the Run/Job/
idempotency record. This module is the only BFF surface for that
conversation-turning operation:

- ``start_turn``: atomic create + idempotent replay;
- ``stop_turn``: thin stop adapter (message_id -> run_id -> cancel command);
- ``get_turn_projection``: fold canonical run events back into user-visible
  content and terminal state.

The authoritative stop endpoint is ``POST /api/v1/runs/{run_id}:cancel``;
``stop_turn`` only exists to keep the legacy message stop endpoint working
without ``StreamRegistry`` semantics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Conversation, IdempotencyRecord, Job, JobStatus, Message, Run
from ..runs import RunApplication, RunCommand
from ..runs.errors import RunTerminalStateError
from ..runtime.state_machine import RunState
from ..services.conversation_service import STREAM_ABORTED
from ..services.idempotency import (
    IdempotencyConflictError,
    IdempotencyService,
)
from .projection import TurnProjection, project_turn_events

TURN_NOT_FOUND = "TURN_NOT_FOUND"
TURN_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TurnError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TurnNotFoundError(TurnError):
    def __init__(self, detail: str) -> None:
        super().__init__(TURN_NOT_FOUND, f"turn not found: {detail}")


@dataclass(frozen=True)
class TurnCreated:
    run_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    status: str
    replayed: bool


@dataclass(frozen=True)
class StopTurnReceipt:
    message_id: uuid.UUID
    run_id: uuid.UUID | None
    accepted: bool
    run_status: str | None
    message_status: str


@dataclass(frozen=True)
class TurnProjectionView:
    run_id: uuid.UUID
    status: str | None
    content: str
    user_message_id: uuid.UUID | None
    assistant_message_id: uuid.UUID | None
    terminal_seen: bool
    last_seq: int


class TurnApplication:
    """Turn-level orchestration; the run lifecycle stays in RunApplication."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_application: RunApplication,
    ) -> None:
        self._session_factory = session_factory
        self._run_application = run_application

    # ------------------------------------------------------------------ start
    async def start_turn(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        conversation_id: uuid.UUID,
        query: str,
        request_id: str,
        idempotency_key: str,
        idempotency_body_hash: str,
    ) -> TurnCreated:
        """Create run + job + message pair + idempotency record atomically.

        Conversation ownership is checked first and a miss is a 404-shaped
        :class:`TurnNotFoundError` (never a differentiator for outsiders).
        Same key + same hash replays the stored triple; same key + different
        hash raises 409 ``IDEMPOTENCY_CONFLICT``.
        """
        now = _utc_now()
        run_id = uuid.uuid4()
        command = RunCommand(
            kind="conversation_turn",
            payload={"query": query, "request_id": request_id},
            snapshot={},
        )
        try:
            async with self._session_factory() as session, session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if (
                    conversation is None
                    or conversation.workspace_id != workspace_id
                    or conversation.owner_user_id != principal_id
                ):
                    raise TurnNotFoundError(str(conversation_id))

                idempotency = IdempotencyService(session)
                replay = await idempotency.lookup(
                    key=idempotency_key,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    request_hash=idempotency_body_hash,
                )
                if replay is not None:
                    return self._turn_from_response_body(replay.response_body)

                session.add(
                    Run(
                        id=run_id,
                        workspace_id=workspace_id,
                        principal_id=principal_id,
                        conversation_id=conversation_id,
                        status=RunState.QUEUED,
                        command_json=command.to_json(),
                        snapshot_json=dict(command.snapshot),
                        last_seq=0,
                        created_at=now,
                    )
                )
                session.add(
                    Job(
                        id=run_id,
                        workspace_id=workspace_id,
                        job_type="run",
                        status=JobStatus.QUEUED,
                        payload_json={
                            "run_id": str(run_id),
                            **command.to_json(),
                        },
                        idempotency_key=idempotency_key,
                        priority=0,
                        attempt=0,
                        next_run_at=now,
                        created_by=principal_id,
                    )
                )
                # Flush the run/job pair first so the messages.run_id FK is
                # satisfied inside the SAME transaction (the dependency is
                # structural and must never be left to ORM insert-order
                # guesses across the map_control schema).
                await session.flush()

                user_message = Message(
                    conversation_id=conversation_id,
                    workspace_id=workspace_id,
                    role="user",
                    status="completed",
                    content=query,
                    request_id=request_id,
                    run_id=run_id,
                    completed_at=now,
                )
                assistant_message = Message(
                    conversation_id=conversation_id,
                    workspace_id=workspace_id,
                    role="assistant",
                    status="streaming",
                    content="",
                    request_id=request_id,
                    run_id=run_id,
                )
                session.add(user_message)
                session.add(assistant_message)
                await session.flush()

                await idempotency.store(
                    key=idempotency_key,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    request_hash=idempotency_body_hash,
                    response_status=201,
                    response_body={
                        "run_id": str(run_id),
                        "user_message_id": str(user_message.id),
                        "assistant_message_id": str(assistant_message.id),
                        "status": RunState.QUEUED,
                    },
                )
                conversation.last_message_at = now
                conversation.version += 1
        except IdempotencyConflictError as exc:
            raise TurnError(
                TURN_IDEMPOTENCY_CONFLICT,
                f"idempotency key {idempotency_key} reused with a different request body",
            ) from exc
        except IntegrityError:
            return await self._replay_turn_after_race(
                workspace_id=workspace_id,
                principal_id=principal_id,
                conversation_id=conversation_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                idempotency_body_hash=idempotency_body_hash,
            )

        return TurnCreated(
            run_id=run_id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            status=RunState.QUEUED,
            replayed=False,
        )

    async def _replay_turn_after_race(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        conversation_id: uuid.UUID,
        request_id: str,
        idempotency_key: str,
        idempotency_body_hash: str,
    ) -> TurnCreated:
        """Recover after a concurrent duplicate key/request_id lost the race."""
        async with self._session_factory() as session:
            record = (
                await session.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.workspace_id == workspace_id,
                        IdempotencyRecord.principal_id == principal_id,
                        IdempotencyRecord.key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if record is not None:
                if record.request_hash != idempotency_body_hash:
                    raise TurnError(
                        TURN_IDEMPOTENCY_CONFLICT,
                        f"idempotency key {idempotency_key} reused with a "
                        "different request body",
                    )
                return self._turn_from_response_body(record.response_body)

            # No idempotency record: the unique assistant request_id partial
            # index won. Recover the existing pair the same way the legacy
            # conversation path does (safe re-query, never a 500).
            existing = (
                await session.execute(
                    select(Message)
                    .join(
                        Conversation,
                        Message.conversation_id == Conversation.id,
                    )
                    .where(
                        Message.request_id == request_id,
                        Message.role == "assistant",
                        Message.workspace_id == workspace_id,
                        Message.conversation_id == conversation_id,
                        Conversation.owner_user_id == principal_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None or existing.run_id is None:
                raise TurnNotFoundError(str(conversation_id))
            user_message = (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.role == "user",
                        Message.request_id == request_id,
                        Message.run_id == existing.run_id,
                    )
                )
            ).scalar_one_or_none()
            run = await session.get(Run, existing.run_id)
            if run is None:
                raise TurnNotFoundError(str(existing.run_id))
            return TurnCreated(
                run_id=existing.run_id,
                user_message_id=user_message.id if user_message is not None else existing.id,
                assistant_message_id=existing.id,
                status=run.status,
                replayed=True,
            )

    @staticmethod
    def _turn_from_response_body(body: dict | None) -> TurnCreated:
        body = body or {}
        try:
            return TurnCreated(
                run_id=uuid.UUID(str(body["run_id"])),
                user_message_id=uuid.UUID(str(body["user_message_id"])),
                assistant_message_id=uuid.UUID(str(body["assistant_message_id"])),
                status=str(body.get("status") or RunState.QUEUED),
                replayed=True,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise TurnError(
                TURN_IDEMPOTENCY_CONFLICT, "stored turn idempotency body is invalid"
            ) from exc

    # ------------------------------------------------------------------- stop
    async def stop_turn(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        message_id: uuid.UUID,
    ) -> StopTurnReceipt | None:
        """Stop a turn by message id (legacy message stop endpoint).

        The canonical cancel command is submitted through
        :class:`RunApplication`; the assistant message is conditionally
        finalized to ``stopped`` exactly as before, but no process-local
        ``StreamRegistry`` semantics are used.
        """
        async with self._session_factory() as session:
            message = await self._find_owned_message(
                session, message_id, workspace_id, principal_id
            )
            if message is None:
                return None
            run_id = message.run_id
            message_status = message.status
            accepted = False
            run_status: str | None = None

            if run_id is None:
                # Legacy message with no canonical run: keep the old
                # conditional terminal write only.
                if message.status == "streaming":
                    await self._finalize_stopped(session, message_id)
                    await session.commit()
                    message_status = "stopped"
            else:
                try:
                    receipt = await self._run_application.cancel_run(
                        workspace_id=workspace_id,
                        principal_id=principal_id,
                        run_id=run_id,
                        reason="stop message",
                    )
                except RunTerminalStateError as exc:
                    run_status = exc.status
                    return StopTurnReceipt(
                        message_id=message_id,
                        run_id=run_id,
                        accepted=False,
                        run_status=run_status,
                        message_status=message_status,
                    )
                accepted = receipt.accepted
                run_status = receipt.status
                if accepted and message.status == "streaming":
                    await self._finalize_stopped(session, message_id)
                    await session.commit()
                    message_status = "stopped"

            return StopTurnReceipt(
                message_id=message_id,
                run_id=run_id,
                accepted=accepted,
                run_status=run_status,
                message_status=message_status,
            )

    async def _find_owned_message(
        self,
        session: AsyncSession,
        message_id: uuid.UUID,
        workspace_id: uuid.UUID,
        owner_user_id: str,
    ) -> Message | None:
        return (
            await session.execute(
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(
                    Message.id == message_id,
                    Message.workspace_id == workspace_id,
                    Conversation.owner_user_id == owner_user_id,
                )
            )
        ).scalar_one_or_none()

    async def _finalize_stopped(
        self, session: AsyncSession, message_id: uuid.UUID
    ) -> None:
        from ..repositories.conversations import ConversationRepository

        await ConversationRepository(session).finalize_message(
            message_id,
            status="stopped",
            stream_error=STREAM_ABORTED,
            error_message="stopped by user",
        )

    # -------------------------------------------------------------- projection
    async def get_turn_projection(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
    ) -> TurnProjectionView | None:
        """Recover a turn's user-visible state from canonical run events."""
        view = await self._run_application.get_run(
            workspace_id=workspace_id,
            principal_id=principal_id,
            run_id=run_id,
        )
        if view is None:
            return None
        events = [
            envelope
            async for envelope in self._run_application.replay_events(
                workspace_id=workspace_id,
                principal_id=principal_id,
                run_id=run_id,
                after_seq=0,
            )
        ]
        projection: TurnProjection = project_turn_events(events)

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.run_id == run_id,
                            Message.workspace_id == workspace_id,
                        )
                        .order_by(Message.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        user_message_id: uuid.UUID | None = None
        assistant_message_id: uuid.UUID | None = None
        for message in rows:
            if message.role == "user" and user_message_id is None:
                user_message_id = message.id
            elif message.role == "assistant" and assistant_message_id is None:
                assistant_message_id = message.id

        return TurnProjectionView(
            run_id=run_id,
            status=projection.terminal_status,
            content=projection.content,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            terminal_seen=projection.terminal_seen,
            last_seq=projection.last_seq,
        )
