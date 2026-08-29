"""PostgreSQL adapter for the Canonical RunStore internal seam.

All invariants live here for the production path:
- create is one transaction: run + 1:1 job + idempotency record;
- claim reuses the EXISTING jobs lease protocol (SKIP LOCKED, database
  clock) - no second lease protocol is introduced;
- append/settle fence on (job.status, lease_owner, attempt,
  lease_expires_at >= now()) before any event or run-status write;
- (run_id, seq) uniqueness is structural and seq assignment is serialized
  by the run row lock, so replay order is exactly commit order.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import IdempotencyRecord, Job, JobStatus, Run, RunEvent
from ..runtime.event_envelope import EventEnvelope
from ..runtime.state_machine import RunState, StateTransitionError, validate_transition
from ..services.idempotency import IdempotencyConflictError, IdempotencyService
from ..services.run_event_stream import envelope_to_row, row_to_envelope
from .domain import (
    CancelReceipt,
    ClaimedRun,
    RunCommand,
    RunCreated,
    RunEventDraft,
    RunView,
)
from .errors import (
    IdempotencyConflictRunError,
    LeaseLostError,
    RunNotFoundError,
    RunStateTransitionError,
)
from .store import CreateRunResult, run_target_for_event

_TERMINAL_JOB_STATUS = {
    RunState.COMPLETED: JobStatus.SUCCEEDED,
    RunState.FAILED: JobStatus.FAILED,
    RunState.CANCELLED: JobStatus.CANCELLED,
    RunState.TIMED_OUT: JobStatus.FAILED,
}

_CANCEL_ALLOWED_FROM = {RunState.QUEUED, RunState.RUNNING, RunState.PAUSED}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _run_view(row: Run) -> RunView:
    command = RunCommand.from_json(row.command_json)
    return RunView(
        run_id=row.id,
        workspace_id=row.workspace_id,
        principal_id=row.principal_id,
        conversation_id=row.conversation_id,
        status=row.status,
        command=command,
        last_seq=row.last_seq,
        cancel_requested=row.cancel_requested_at is not None,
        error_code=row.error_code,
        runtime_snapshot_id=row.runtime_snapshot_id,
        runtime_snapshot_digest=row.runtime_snapshot_digest,
    )


class PgRunStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ create
    async def create_run(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        conversation_id: uuid.UUID | None,
        command: RunCommand,
        runtime_snapshot_id: uuid.UUID,
        runtime_snapshot_digest: str,
        idempotency_key: str,
        idempotency_body_hash: str,
        now: datetime | None = None,
    ) -> CreateRunResult:
        now = now or _utc_now()
        run_id = uuid.uuid4()
        replay_run_id: uuid.UUID | None = None
        try:
            async with self._session_factory() as session, session.begin():
                idempotency = IdempotencyService(session)
                replay = await idempotency.lookup(
                    key=idempotency_key,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    request_hash=idempotency_body_hash,
                )
                if replay is not None:
                    replay_run_id = uuid.UUID(
                        (replay.response_body or {}).get("run_id", "")
                    )
                else:
                    session.add(
                        Run(
                            id=run_id,
                            workspace_id=workspace_id,
                            principal_id=principal_id,
                            conversation_id=conversation_id,
                            status=RunState.QUEUED,
                            command_json=command.to_json(),
                            snapshot_json=dict(command.snapshot),
                            runtime_snapshot_id=runtime_snapshot_id,
                            runtime_snapshot_digest=runtime_snapshot_digest,
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
                    await idempotency.store(
                        key=idempotency_key,
                        workspace_id=workspace_id,
                        principal_id=principal_id,
                        request_hash=idempotency_body_hash,
                        response_status=201,
                        response_body={
                            "run_id": str(run_id),
                            "status": RunState.QUEUED,
                        },
                    )
        except IdempotencyConflictError as exc:
            raise IdempotencyConflictRunError(idempotency_key) from exc
        except IntegrityError:
            return await self._replay_after_race(
                workspace_id=workspace_id,
                principal_id=principal_id,
                idempotency_key=idempotency_key,
                idempotency_body_hash=idempotency_body_hash,
            )

        target_id = replay_run_id or run_id
        async with self._session_factory() as session:
            row = await session.get(Run, target_id)
            if row is None or row.workspace_id != workspace_id:
                raise RunNotFoundError(str(target_id))
            view = _run_view(row)
        return CreateRunResult(
            RunCreated(
                run_id=target_id,
                status=row.status,
                replayed=replay_run_id is not None,
            ),
            view,
        )

    async def _replay_after_race(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        idempotency_key: str,
        idempotency_body_hash: str,
    ) -> CreateRunResult:
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
            if record is None:
                raise RuntimeError("idempotency race lost but no record found")
            if record.request_hash != idempotency_body_hash:
                raise IdempotencyConflictRunError(idempotency_key)
            replay_run_id = uuid.UUID((record.response_body or {}).get("run_id", ""))
            row = await session.get(Run, replay_run_id)
            if row is None or row.workspace_id != workspace_id:
                raise RunNotFoundError(str(replay_run_id))
            return CreateRunResult(
                RunCreated(run_id=row.id, status=row.status, replayed=True),
                _run_view(row),
            )

    # ------------------------------------------------------------------- read
    async def get_run_view(
        self, *, workspace_id: uuid.UUID, principal_id: str, run_id: uuid.UUID
    ) -> RunView | None:
        async with self._session_factory() as session:
            row = await session.get(Run, run_id)
            if (
                row is None
                or row.workspace_id != workspace_id
                or row.principal_id != principal_id
            ):
                return None
            return _run_view(row)

    # ----------------------------------------------------------------- cancel
    async def submit_cancel_command(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        reason: str,
        now: datetime | None = None,
    ) -> CancelReceipt | None:
        now = now or _utc_now()
        receipt: CancelReceipt | None = None
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.workspace_id == workspace_id,
                    Run.status.in_(_CANCEL_ALLOWED_FROM),
                    Run.cancel_requested_at.is_(None),
                )
                .values(cancel_requested_at=now, cancel_reason=reason)
            )
            row = await session.get(Run, run_id)
            if row is None or row.workspace_id != workspace_id:
                receipt = None
            elif result.rowcount == 0:
                receipt = CancelReceipt(
                    run_id=run_id, accepted=False, status=row.status
                )
            else:
                receipt = CancelReceipt(
                    run_id=run_id, accepted=True, status=row.status
                )
        return receipt

    async def has_cancel_request(
        self, *, claim: ClaimedRun, now: datetime | None = None
    ) -> bool:
        del now  # the cancel-command fact is independent of the virtual clock
        async with self._session_factory() as session:
            row = await session.get(Run, claim.run_id)
            if row is None or row.workspace_id != claim.workspace_id:
                raise RunNotFoundError(str(claim.run_id))
            return row.cancel_requested_at is not None

    # ------------------------------------------------------------------ claim
    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> ClaimedRun | None:
        now = now or _utc_now()
        claimed: ClaimedRun | None = None
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(Job)
                .join(Run, Run.id == Job.id)
                .where(
                    Job.job_type == "run",
                    or_(
                        (Job.status == JobStatus.QUEUED)
                        & (Job.next_run_at <= now),
                        (Job.status == JobStatus.RUNNING)
                        & (Job.lease_expires_at < now),
                    ),
                )
                .with_for_update(skip_locked=True)
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if job is not None:
                run = await session.get(Run, job.id, with_for_update=True)
                if run is not None:
                    attempt = job.attempt + 1
                    job.status = JobStatus.RUNNING
                    job.attempt = attempt
                    job.lease_owner = worker_id
                    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    if job.started_at is None:
                        job.started_at = now
                    claimed = ClaimedRun(
                        run_id=run.id,
                        workspace_id=run.workspace_id,
                        principal_id=run.principal_id,
                        attempt=attempt,
                        command=RunCommand.from_json(run.command_json),
                        last_seq=run.last_seq,
                        worker_id=worker_id,
                        lease_expires_at=job.lease_expires_at,
                        lease_seconds=lease_seconds,
                        max_attempts=job.max_attempts or 3,
                    )
        return claimed

    async def heartbeat(
        self,
        *,
        claim: ClaimedRun,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or _utc_now()
        ok = False
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(Job)
                .where(
                    Job.id == claim.run_id,
                    Job.status == JobStatus.RUNNING,
                    Job.lease_owner == claim.worker_id,
                    Job.attempt == claim.attempt,
                    Job.lease_expires_at >= now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    next_run_at=None,
                )
            )
            ok = result.rowcount == 1
        return ok

    # ----------------------------------------------------------------- append
    async def append_events(
        self,
        *,
        claim: ClaimedRun,
        drafts: Sequence[RunEventDraft],
        now: datetime | None = None,
    ) -> tuple[EventEnvelope, ...]:
        if not drafts:
            return ()
        now = now or _utc_now()
        envelopes: list[EventEnvelope] = []
        async with self._session_factory() as session, session.begin():
            run = await self._fenced_run(session, claim, now)
            seq = run.last_seq
            for draft in drafts:
                if run_target_for_event(draft.type) is not None:
                    raise ValueError(
                        f"run-state event {draft.type!r} must go through settle_terminal"
                    )
                seq += 1
                envelope = EventEnvelope.build(
                    run_id=str(run.id),
                    seq=seq,
                    event_type=draft.type,
                    workspace_id=str(run.workspace_id),
                    data=draft.data or {},
                    occurred_at=now.isoformat(),
                )
                self._insert_event(session, run, envelope, now)
                envelopes.append(envelope)
            run.last_seq = seq
            run.updated_at = now
        return tuple(envelopes)

    async def fail_attempt(
        self,
        *,
        claim: ClaimedRun,
        error_code: str,
        error_message: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> bool:
        """Return True when the attempt failure was scheduled for retry.

        Reuses the existing jobs.max_attempts field and backoff shape
        (2 ** attempt seconds) as the single retry fact source.
        """
        now = now or _utc_now()
        scheduled = False
        async with self._session_factory() as session, session.begin():
            job = await session.get(Job, claim.run_id, with_for_update=True)
            if (
                job is None
                or job.status != JobStatus.RUNNING
                or job.lease_owner != claim.worker_id
                or job.attempt != claim.attempt
                or job.lease_expires_at is None
                or job.lease_expires_at < now
            ):
                raise LeaseLostError(str(claim.run_id), claim.attempt)
            scheduled = retryable and claim.attempt < (job.max_attempts or 3)
            if scheduled:
                job.error_code = error_code
                job.error_message = error_message
                job.lease_owner = None
                job.lease_expires_at = None
                job.started_at = None
                job.status = JobStatus.QUEUED
                job.next_run_at = now + timedelta(seconds=2 ** claim.attempt)
                job.finished_at = None
            # When retry is exhausted the job row stays RUNNING with its
            # lease; the caller MUST settle the run terminal in the same
            # attempt (settle_terminal updates the job in its transaction).
        return scheduled

    async def settle_terminal(
        self,
        *,
        claim: ClaimedRun,
        event_type: str,
        data: dict | None,
        now: datetime | None = None,
    ) -> EventEnvelope:
        target = run_target_for_event(event_type)
        if target is None:
            raise ValueError(f"{event_type!r} is not a run-state event")
        now = now or _utc_now()
        envelope: EventEnvelope | None = None
        async with self._session_factory() as session, session.begin():
            job = await session.get(Job, claim.run_id, with_for_update=True)
            run = await session.get(Run, claim.run_id, with_for_update=True)
            if run is None or run.workspace_id != claim.workspace_id:
                raise RunNotFoundError(str(claim.run_id))
            try:
                validate_transition("run", run.status, target)
            except StateTransitionError as exc:
                raise RunStateTransitionError(run.status, target) from exc
            # The run row is locked; re-check the lease fence only after
            # the state transition is known to be legal so a terminal race
            # reports STATE_TRANSITION_VIOLATION instead of LEASE_LOST.
            if (
                job is None
                or job.status != JobStatus.RUNNING
                or job.lease_owner != claim.worker_id
                or job.attempt != claim.attempt
                or job.lease_expires_at is None
                or job.lease_expires_at < now
            ):
                raise LeaseLostError(str(claim.run_id), claim.attempt)
            envelope = EventEnvelope.build(
                run_id=str(run.id),
                seq=run.last_seq + 1,
                event_type=event_type,
                workspace_id=str(run.workspace_id),
                data=data or {},
                occurred_at=now.isoformat(),
            )
            self._insert_event(session, run, envelope, now)
            run.status = target
            run.last_seq = envelope.seq
            run.updated_at = now
            if target == RunState.RUNNING and run.started_at is None:
                run.started_at = now
            if target in _TERMINAL_JOB_STATUS:
                run.finished_at = now
                if target in (RunState.FAILED, RunState.TIMED_OUT):
                    run.error_code = data.get("code") if data else None
                    run.error_message = data.get("message") if data else None
                job.status = _TERMINAL_JOB_STATUS[target]
                job.finished_at = now
                job.error_code = run.error_code
                job.error_message = run.error_message
        assert envelope is not None
        return envelope

    async def _fenced_run(
        self, session: AsyncSession, claim: ClaimedRun, now: datetime
    ) -> Run:
        job = await session.get(Job, claim.run_id, with_for_update=True)
        if (
            job is None
            or job.status != JobStatus.RUNNING
            or job.lease_owner != claim.worker_id
            or job.attempt != claim.attempt
            or job.lease_expires_at is None
            or job.lease_expires_at < now
        ):
            raise LeaseLostError(str(claim.run_id), claim.attempt)
        run = await session.get(Run, claim.run_id, with_for_update=True)
        if run is None or run.workspace_id != claim.workspace_id:
            raise RunNotFoundError(str(claim.run_id))
        return run

    def _insert_event(
        self, session: AsyncSession, run: Run, envelope: EventEnvelope, now: datetime
    ) -> None:
        del now
        row = envelope_to_row(envelope)
        occurred_at = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        session.add(
            RunEvent(
                run_id=run.id,
                seq=envelope.seq,
                event_id=uuid.UUID(envelope.event_id),
                event_type=envelope.type,
                occurred_at=occurred_at,
                workspace_id=run.workspace_id,
                schema_version=envelope.schema_version,
                schema_minor=envelope.schema_minor,
                payload_json=row["payload_json"],
            )
        )

    async def read_events_after(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        after_seq: int,
    ) -> AsyncIterator[EventEnvelope]:
        async with self._session_factory() as session:
            owner = await session.get(Run, run_id)
            if (
                owner is None
                or owner.workspace_id != workspace_id
                or owner.principal_id != principal_id
            ):
                raise RunNotFoundError(str(run_id))
            cursor = after_seq
            while True:
                rows = (
                    (
                        await session.execute(
                            select(RunEvent)
                            .where(
                                RunEvent.run_id == run_id,
                                RunEvent.workspace_id == workspace_id,
                                RunEvent.seq > cursor,
                            )
                            .order_by(RunEvent.seq)
                            .limit(500)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    return
                for row in rows:
                    cursor = row.seq
                    yield row_to_envelope(
                        {
                            "run_id": str(row.run_id),
                            "seq": row.seq,
                            "event_id": str(row.event_id),
                            "event_type": row.event_type,
                            "occurred_at": row.occurred_at.isoformat(),
                            "workspace_id": str(row.workspace_id),
                            "schema_version": row.schema_version,
                            "schema_minor": row.schema_minor,
                            "payload_json": row.payload_json,
                        }
                    )
