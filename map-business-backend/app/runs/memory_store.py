"""Deterministic in-memory adapter for the RunStore internal seam.

This is a REAL second adapter, not a toy: every semantic the PG adapter
guarantees has an equivalent here, under one asyncio lock and an explicit
clock:

- claim picks exactly one run and models SKIP LOCKED (atomic selection);
- lease expiry is compared against the injected clock;
- settle is a CAS (the transition is validated against the current status
  under the lock, so a loser never creates an out-of-table state);
- (run_id, seq) is unique and strictly sequential.

SQLite is not an adapter: it cannot model these semantics.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..runtime.event_envelope import EventEnvelope
from ..runtime.state_machine import RunState, StateTransitionError, validate_transition
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

_CANCEL_ALLOWED_FROM = {RunState.QUEUED, RunState.RUNNING, RunState.PAUSED}
_TERMINAL_JOB_STATUS = {
    RunState.COMPLETED: "succeeded",
    RunState.FAILED: "failed",
    RunState.CANCELLED: "cancelled",
    RunState.TIMED_OUT: "failed",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _MemoryRun:
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: str
    conversation_id: uuid.UUID | None
    status: str
    command: RunCommand
    last_seq: int = 0
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 1:1 job protocol state (the same single lease protocol as PG).
    job_status: str = "queued"
    attempt: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_seconds: int = 0
    max_attempts: int = 3
    next_run_at: datetime | None = None
    events: list[EventEnvelope] = field(default_factory=list)


class InMemoryRunStore:
    def __init__(self, now: datetime | None = None) -> None:
        self._runs: dict[uuid.UUID, _MemoryRun] = {}
        self._order: list[uuid.UUID] = []
        self._idempotency: dict[tuple[uuid.UUID, str, str], tuple[str, uuid.UUID]] = {}
        self._lock = asyncio.Lock()
        self._clock = now or _utc_now()

    def set_clock(self, now: datetime) -> None:
        """Advance the deterministic clock (test-only internal seam usage)."""
        self._clock = now

    @property
    def now(self) -> datetime:
        return self._clock

    def _view(self, run: _MemoryRun) -> RunView:
        return RunView(
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            principal_id=run.principal_id,
            conversation_id=run.conversation_id,
            status=run.status,
            command=run.command,
            last_seq=run.last_seq,
            cancel_requested=run.cancel_requested_at is not None,
            error_code=run.error_code,
        )

    # ------------------------------------------------------------------ create
    async def create_run(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        conversation_id: uuid.UUID | None,
        command: RunCommand,
        idempotency_key: str,
        idempotency_body_hash: str,
        now: datetime | None = None,
    ) -> CreateRunResult:
        now = now or self._clock
        key = (workspace_id, principal_id, idempotency_key)
        async with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                stored_hash, run_id = existing
                if stored_hash != idempotency_body_hash:
                    raise IdempotencyConflictRunError(idempotency_key)
                run = self._runs[run_id]
                return CreateRunResult(
                    RunCreated(run_id=run.run_id, status=run.status, replayed=True),
                    self._view(run),
                )
            run_id = uuid.uuid4()
            run = _MemoryRun(
                run_id=run_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                conversation_id=conversation_id,
                status=RunState.QUEUED,
                command=command,
                created_at=now,
                next_run_at=now,
            )
            self._runs[run_id] = run
            self._order.append(run_id)
            self._idempotency[key] = (idempotency_body_hash, run_id)
            return CreateRunResult(
                RunCreated(run_id=run_id, status=RunState.QUEUED, replayed=False),
                self._view(run),
            )

    async def get_run_view(
        self, *, workspace_id: uuid.UUID, principal_id: str, run_id: uuid.UUID
    ) -> RunView | None:
        async with self._lock:
            run = self._runs.get(run_id)
            if (
                run is None
                or run.workspace_id != workspace_id
                or run.principal_id != principal_id
            ):
                return None
            return self._view(run)

    async def submit_cancel_command(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        reason: str,
        now: datetime | None = None,
    ) -> CancelReceipt | None:
        now = now or self._clock
        async with self._lock:
            run = self._runs.get(run_id)
            if (
                run is None
                or run.workspace_id != workspace_id
                or run.principal_id != principal_id
            ):
                return None
            if run.status in _CANCEL_ALLOWED_FROM and run.cancel_requested_at is None:
                run.cancel_requested_at = now
                run.cancel_reason = reason
                return CancelReceipt(run_id=run_id, accepted=True, status=run.status)
            return CancelReceipt(run_id=run_id, accepted=False, status=run.status)

    async def has_cancel_request(
        self, *, claim: ClaimedRun, now: datetime | None = None
    ) -> bool:
        del now
        async with self._lock:
            run = self._runs.get(claim.run_id)
            if run is None or run.workspace_id != claim.workspace_id:
                raise RunNotFoundError(str(claim.run_id))
            return run.cancel_requested_at is not None

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> ClaimedRun | None:
        now = now or self._clock
        async with self._lock:
            for run_id in self._order:
                run = self._runs[run_id]
                queued = (
                    run.job_status == "queued"
                    and run.next_run_at is not None
                    and run.next_run_at <= now
                )
                expired = (
                    run.job_status == "running"
                    and run.lease_expires_at is not None
                    and run.lease_expires_at < now
                )
                if not (queued or expired):
                    continue
                run.attempt += 1
                run.job_status = "running"
                run.lease_owner = worker_id
                run.lease_seconds = lease_seconds
                run.lease_expires_at = now + timedelta(seconds=lease_seconds)
                run.next_run_at = None
                if run.started_at is None:
                    run.started_at = now
                return ClaimedRun(
                    run_id=run.run_id,
                    workspace_id=run.workspace_id,
                    principal_id=run.principal_id,
                    attempt=run.attempt,
                    command=run.command,
                    last_seq=run.last_seq,
                    worker_id=worker_id,
                    lease_expires_at=run.lease_expires_at,
                    lease_seconds=lease_seconds,
                    max_attempts=run.max_attempts,
                )
            return None

    async def heartbeat(
        self,
        *,
        claim: ClaimedRun,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or self._clock
        async with self._lock:
            run = self._runs.get(claim.run_id)
            if not self._lease_valid(run, claim, now):
                return False
            run.lease_seconds = lease_seconds
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return True

    def _lease_valid(self, run: _MemoryRun | None, claim: ClaimedRun, now: datetime) -> bool:
        return (
            run is not None
            and run.job_status == "running"
            and run.lease_owner == claim.worker_id
            and run.attempt == claim.attempt
            and run.lease_expires_at is not None
            and run.lease_expires_at >= now
        )

    async def append_events(
        self,
        *,
        claim: ClaimedRun,
        drafts: Sequence[RunEventDraft],
        now: datetime | None = None,
    ) -> tuple[EventEnvelope, ...]:
        if not drafts:
            return ()
        now = now or self._clock
        async with self._lock:
            run = self._fenced(claim, now)
            envelopes = []
            for draft in drafts:
                if run_target_for_event(draft.type) is not None:
                    raise ValueError(
                        f"run-state event {draft.type!r} must go through settle_terminal"
                    )
                envelope = self._build_event(run, draft.type, draft.data, now)
                run.events.append(envelope)
                run.last_seq = envelope.seq
                envelopes.append(envelope)
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
        now = now or self._clock
        async with self._lock:
            run = self._runs.get(claim.run_id)
            if not self._lease_valid(run, claim, now):
                raise LeaseLostError(str(claim.run_id), claim.attempt)
            scheduled = retryable and claim.attempt < run.max_attempts
            if scheduled:
                run.lease_owner = None
                run.lease_expires_at = None
                run.lease_seconds = 0
                run.job_status = "queued"
                run.next_run_at = now + timedelta(seconds=2 ** claim.attempt)
            # Exhausted retry: keep the lease so the caller can settle the
            # run terminal in the same attempt.
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
        now = now or self._clock
        async with self._lock:
            run = self._fenced(claim, now)
            try:
                validate_transition("run", run.status, target)
            except StateTransitionError as exc:
                raise RunStateTransitionError(run.status, target) from exc
            envelope = self._build_event(run, event_type, data, now)
            run.events.append(envelope)
            run.status = target
            run.last_seq = envelope.seq
            if target == RunState.RUNNING and run.started_at is None:
                run.started_at = now
            if target in _TERMINAL_JOB_STATUS:
                run.finished_at = now
                if target in (RunState.FAILED, RunState.TIMED_OUT):
                    run.error_code = (data or {}).get("code")
                run.job_status = _TERMINAL_JOB_STATUS[target]
            return envelope

    def _fenced(self, claim: ClaimedRun, now: datetime) -> _MemoryRun:
        run = self._runs.get(claim.run_id)
        if not self._lease_valid(run, claim, now):
            raise LeaseLostError(str(claim.run_id), claim.attempt)
        if run.workspace_id != claim.workspace_id:
            raise RunNotFoundError(str(claim.run_id))
        return run

    def _build_event(
        self, run: _MemoryRun, event_type: str, data: dict | None, now: datetime
    ) -> EventEnvelope:
        return EventEnvelope.build(
            run_id=str(run.run_id),
            seq=run.last_seq + 1,
            event_type=event_type,
            workspace_id=str(run.workspace_id),
            data=data or {},
            occurred_at=now.isoformat(),
        )

    async def read_events_after(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        after_seq: int,
    ) -> AsyncIterator[EventEnvelope]:
        async with self._lock:
            run = self._runs.get(run_id)
            if (
                run is None
                or run.workspace_id != workspace_id
                or run.principal_id != principal_id
            ):
                raise RunNotFoundError(str(run_id))
            events = [e for e in run.events if e.seq > after_seq]
        for event in events:
            yield event
