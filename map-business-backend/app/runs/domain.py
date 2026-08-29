"""Domain types for the Canonical Run module (Step 2 / PR-C).

These are the facts a caller must learn to use the module - nothing here may
leak table shapes, lease mechanics or transaction boundaries. Pure frozen
values only; no I/O.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeAlias

JsonObject: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True)
class RunCommand:
    """The durable execution command stored atomically with the Run.

    ``snapshot`` is the already-frozen Runtime Snapshot the Run must be
    interpreted against (P1-CONFIG-01); PR-C treats it as an opaque value.
    """

    kind: Literal["conversation_turn", "sandbox_invocation"]
    payload: JsonObject
    snapshot: JsonObject

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": dict(self.payload),
            "snapshot": dict(self.snapshot),
        }

    @classmethod
    def from_json(cls, value: Any) -> RunCommand:
        if not isinstance(value, dict):
            raise TypeError("RunCommand JSON must be an object")
        kind = value.get("kind")
        if kind not in ("conversation_turn", "sandbox_invocation"):
            raise ValueError(f"unsupported RunCommand kind: {kind!r}")
        payload = value.get("payload")
        snapshot = value.get("snapshot")
        if not isinstance(payload, dict) or not isinstance(snapshot, dict):
            raise ValueError("RunCommand payload/snapshot must be objects")
        return cls(kind=kind, payload=payload, snapshot=snapshot)


@dataclass(frozen=True)
class RunCreated:
    run_id: uuid.UUID
    status: str
    replayed: bool


@dataclass(frozen=True)
class CancelReceipt:
    run_id: uuid.UUID
    accepted: bool
    status: str


@dataclass(frozen=True)
class RunView:
    """Read projection returned by ``get_run`` (never a mutable row)."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: str
    conversation_id: uuid.UUID | None
    status: str
    command: RunCommand
    last_seq: int
    cancel_requested: bool
    error_code: str | None
    runtime_snapshot_id: uuid.UUID | None
    runtime_snapshot_digest: str | None


@dataclass(frozen=True)
class CoreEvent:
    """A typed execution fact produced by core (never a run.*/attempt.* event)."""

    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class CoreOutcome:
    """Core-declared terminal result for this attempt."""

    status: Literal["completed", "failed"]
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CoreError:
    """Transport-level failure of the core stream (not a core verdict)."""

    code: str
    message: str


CoreItem: TypeAlias = CoreEvent | CoreOutcome | CoreError


@dataclass(frozen=True)
class AttemptInput:
    """Everything a handler may know about the attempt it is executing."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    attempt: int
    command: RunCommand


RunAttemptHandler: TypeAlias = Callable[[AttemptInput], AsyncIterator[CoreItem]]


@dataclass(frozen=True)
class RunEventDraft:
    """One event the worker wants to append (validated before any write)."""

    type: str
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClaimedRun:
    """A lease-holding claim token (internal seam value, never a caller type)."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: str
    attempt: int
    command: RunCommand
    last_seq: int
    worker_id: str
    lease_expires_at: datetime
    lease_seconds: int
    max_attempts: int


@dataclass(frozen=True)
class AdvanceOutcome:
    run_id: uuid.UUID
    attempt: int
    run_status: str
    events_appended: int
    attempt_retryable: bool | None = None
