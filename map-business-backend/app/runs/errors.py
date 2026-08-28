"""Typed errors of the Canonical Run module.

Callers only see codes that already exist in ``app/runtime/error_mapping.py``
(or the frozen state/envelope errors); no new wire code is invented here.
"""

from __future__ import annotations

from ..runtime.event_envelope import (
    EVENT_STALE_SEQ,
    IDEMPOTENCY_CONFLICT,
)
from ..runtime.state_machine import (
    RUN_TERMINAL_STATE,
    STATE_TRANSITION_VIOLATION,
)


class RunError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class RunNotFoundError(RunError):
    def __init__(self, run_id: str) -> None:
        super().__init__("RUN_NOT_FOUND", f"run not found: {run_id}")


class LeaseLostError(RunError):
    def __init__(self, run_id: str, attempt: int) -> None:
        super().__init__(
            "LEASE_LOST",
            f"run {run_id} attempt {attempt} no longer owns the lease",
        )


class IdempotencyConflictRunError(RunError):
    def __init__(self, key: str) -> None:
        super().__init__(
            IDEMPOTENCY_CONFLICT,
            f"idempotency key {key} reused with a different request body",
        )


class RunTerminalStateError(RunError):
    def __init__(self, run_id: str, status: str) -> None:
        self.status = status
        super().__init__(
            RUN_TERMINAL_STATE,
            f"run {run_id} is already terminal: {status}",
        )


class RunStateTransitionError(RunError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            STATE_TRANSITION_VIOLATION,
            f"run cannot transition {current} -> {target}",
        )


class RunEventStaleSeqError(RunError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            EVENT_STALE_SEQ,
            f"event sequence mismatch: expected {expected}, got {actual}",
        )


LEASE_LOST = "LEASE_LOST"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
