"""Internal seam of the Canonical Run module.

``RunStore`` is deliberately NOT part of the caller-facing interface: the
BFF surface is :class:`app.runs.application.RunApplication` and the worker
surface is :class:`app.runs.attempt.RunWorker`. Callers never learn these
methods or the fact that runs are linked 1:1 to jobs.

Two adapters implement this exact contract:
- :class:`app.runs.pg_store.PgRunStore` (production PostgreSQL;
  SKIP LOCKED, database clock, unique constraints);
- :class:`app.runs.memory_store.InMemoryRunStore` (deterministic tests;
  a single lock plus an explicit virtual clock models the same semantics).

SQLite is intentionally not an adapter: it cannot model SKIP LOCKED,
database clocks or the CAS/fencing behavior this contract requires.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol

from ..runtime.event_envelope import EventEnvelope
from ..runtime.state_machine import RunState
from .domain import (
    CancelReceipt,
    ClaimedRun,
    RunCommand,
    RunCreated,
    RunEventDraft,
    RunView,
)

# Frozen projection from canonical run.* event types to the run status they
# settle. Events not listed here never move the run row; they are still
# appended when their envelope is valid.
_RUN_EVENT_TARGET: dict[str, str] = {
    "run.started": RunState.RUNNING,
    "run.completed": RunState.COMPLETED,
    "run.failed": RunState.FAILED,
    "run.cancelling": RunState.CANCELLING,
    "run.cancelled": RunState.CANCELLED,
    "run.timed_out": RunState.TIMED_OUT,
}


def run_target_for_event(event_type: str) -> str | None:
    """Run-state target owned by a canonical run.* event, if any."""
    return _RUN_EVENT_TARGET.get(event_type)


class CreateRunResult:
    """Store-level result of the atomic create (not a wire type)."""

    def __init__(self, created: RunCreated, run_view: RunView) -> None:
        self.created = created
        self.run_view = run_view


class RunStore(Protocol):
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
    ) -> CreateRunResult: ...

    async def get_run_view(
        self, *, workspace_id: uuid.UUID, principal_id: str, run_id: uuid.UUID
    ) -> RunView | None: ...

    async def submit_cancel_command(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        reason: str,
        now: datetime | None = None,
    ) -> CancelReceipt | None: ...

    async def has_cancel_request(
        self, *, claim: ClaimedRun, now: datetime | None = None
    ) -> bool: ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> ClaimedRun | None: ...

    async def heartbeat(
        self,
        *,
        claim: ClaimedRun,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool: ...

    async def append_events(
        self,
        *,
        claim: ClaimedRun,
        drafts: Sequence[RunEventDraft],
        now: datetime | None = None,
    ) -> tuple[EventEnvelope, ...]: ...

    async def fail_attempt(
        self,
        *,
        claim: ClaimedRun,
        error_code: str,
        error_message: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> bool: ...

    async def settle_terminal(
        self,
        *,
        claim: ClaimedRun,
        event_type: str,
        data: dict | None,
        now: datetime | None = None,
    ) -> EventEnvelope: ...

    async def read_events_after(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        after_seq: int,
    ) -> AsyncIterator[EventEnvelope]: ...
