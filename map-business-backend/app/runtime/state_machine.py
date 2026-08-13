"""Canonical Run/Step/Effect/ModelInvocation/Evidence state machines.

P0-CONTRACT-01 (ADR-0002): the frozen transition tables below are the single
source of truth for state transitions. They mirror the ``canonical_states``
block of ``TODO/acceptance-profile.yaml``; every illegal transition and every
out-edge of a terminal state must fail closed with ``STATE_TRANSITION_VIOLATION``
*before* any write happens.

The module is dependency-free (no DB, no FastAPI) so it can be unit-tested in
isolation and reused by BFF routers, workers and future core-side validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

STATE_TRANSITION_VIOLATION = "STATE_TRANSITION_VIOLATION"
RUN_TERMINAL_STATE = "RUN_TERMINAL_STATE"


class StateTransitionError(ValueError):
    """Raised when a transition is not allowed by the frozen table."""

    def __init__(self, machine: str, current: str, target: str) -> None:
        self.machine = machine
        self.current = current
        self.target = target
        super().__init__(
            f"{STATE_TRANSITION_VIOLATION}: {machine} cannot transition "
            f"'{current}' -> '{target}'"
        )


class RunState:
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class StepState:
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class EffectState:
    PLANNED = "planned"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    RECONCILING = "reconciling"
    CANCELLED = "cancelled"


class ModelInvocationState:
    PLANNED = "planned"
    SENT = "sent"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


class EvidenceState:
    NOT_RUN = "not-run"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    NOT_APPLICABLE_APPROVED = "not-applicable-approved"


# canonical_states 全量枚举（与 acceptance-profile.yaml 一一对应，用于
# 迁移/对账测试的锚点）。
CANONICAL_STATES: Final[dict[str, tuple[str, ...]]] = {
    "run": (
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.PAUSED,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLING,
        RunState.CANCELLED,
        RunState.TIMED_OUT,
    ),
    "step": (
        StepState.PENDING,
        StepState.READY,
        StepState.RUNNING,
        StepState.WAITING_APPROVAL,
        StepState.SUCCEEDED,
        StepState.FAILED,
        StepState.SKIPPED,
        StepState.CANCELLED,
    ),
    "effect": (
        EffectState.PLANNED,
        EffectState.APPROVAL_REQUIRED,
        EffectState.APPROVED,
        EffectState.EXECUTING,
        EffectState.SUCCEEDED,
        EffectState.FAILED,
        EffectState.UNCERTAIN,
        EffectState.RECONCILING,
        EffectState.CANCELLED,
    ),
    "model_invocation": (
        ModelInvocationState.PLANNED,
        ModelInvocationState.SENT,
        ModelInvocationState.SUCCEEDED,
        ModelInvocationState.FAILED,
        ModelInvocationState.UNKNOWN,
        ModelInvocationState.RECONCILED,
    ),
    "evidence": (
        EvidenceState.NOT_RUN,
        EvidenceState.RUNNING,
        EvidenceState.PASS,
        EvidenceState.FAIL,
        EvidenceState.BLOCKED,
        EvidenceState.NOT_APPLICABLE_APPROVED,
    ),
}

TERMINAL_STATES: Final[dict[str, frozenset[str]]] = {
    "run": frozenset(
        {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT}
    ),
    "step": frozenset(
        {StepState.SUCCEEDED, StepState.FAILED, StepState.SKIPPED, StepState.CANCELLED}
    ),
    "effect": frozenset(
        {EffectState.SUCCEEDED, EffectState.FAILED, EffectState.CANCELLED}
    ),
    "model_invocation": frozenset(
        {
            ModelInvocationState.SUCCEEDED,
            ModelInvocationState.FAILED,
            ModelInvocationState.RECONCILED,
        }
    ),
    "evidence": frozenset(
        {
            EvidenceState.PASS,
            EvidenceState.FAIL,
            EvidenceState.BLOCKED,
            EvidenceState.NOT_APPLICABLE_APPROVED,
        }
    ),
}

TRANSITIONS: Final[dict[str, dict[str, frozenset[str]]]] = {
    "run": {
        RunState.QUEUED: frozenset(
            {
                RunState.RUNNING,
                RunState.CANCELLING,
                RunState.CANCELLED,
                RunState.TIMED_OUT,
            }
        ),
        RunState.RUNNING: frozenset(
            {
                RunState.PAUSED,
                RunState.COMPLETED,
                RunState.FAILED,
                RunState.CANCELLING,
                RunState.TIMED_OUT,
            }
        ),
        RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLING}),
        RunState.CANCELLING: frozenset({RunState.CANCELLED}),
    },
    "step": {
        StepState.PENDING: frozenset(
            {StepState.READY, StepState.SKIPPED, StepState.CANCELLED}
        ),
        StepState.READY: frozenset(
            {StepState.RUNNING, StepState.SKIPPED, StepState.CANCELLED}
        ),
        StepState.RUNNING: frozenset(
            {
                StepState.WAITING_APPROVAL,
                StepState.SUCCEEDED,
                StepState.FAILED,
                StepState.CANCELLED,
            }
        ),
        StepState.WAITING_APPROVAL: frozenset(
            {StepState.RUNNING, StepState.FAILED, StepState.CANCELLED}
        ),
    },
    "effect": {
        EffectState.PLANNED: frozenset(
            {EffectState.APPROVAL_REQUIRED, EffectState.EXECUTING, EffectState.CANCELLED}
        ),
        EffectState.APPROVAL_REQUIRED: frozenset(
            {EffectState.APPROVED, EffectState.CANCELLED}
        ),
        EffectState.APPROVED: frozenset({EffectState.EXECUTING, EffectState.CANCELLED}),
        EffectState.EXECUTING: frozenset(
            {
                EffectState.SUCCEEDED,
                EffectState.FAILED,
                EffectState.UNCERTAIN,
                EffectState.CANCELLED,
            }
        ),
        EffectState.UNCERTAIN: frozenset({EffectState.RECONCILING}),
        EffectState.RECONCILING: frozenset(
            {EffectState.SUCCEEDED, EffectState.FAILED}
        ),
    },
    "model_invocation": {
        ModelInvocationState.PLANNED: frozenset(
            {ModelInvocationState.SENT, ModelInvocationState.FAILED}
        ),
        ModelInvocationState.SENT: frozenset(
            {
                ModelInvocationState.SUCCEEDED,
                ModelInvocationState.FAILED,
                ModelInvocationState.UNKNOWN,
            }
        ),
        ModelInvocationState.UNKNOWN: frozenset({ModelInvocationState.RECONCILED}),
    },
    "evidence": {
        EvidenceState.NOT_RUN: frozenset({EvidenceState.RUNNING}),
        EvidenceState.RUNNING: frozenset(
            {
                EvidenceState.PASS,
                EvidenceState.FAIL,
                EvidenceState.BLOCKED,
                EvidenceState.NOT_APPLICABLE_APPROVED,
            }
        ),
    },
}


@dataclass(frozen=True)
class Transition:
    machine: str
    current: str
    target: str


def is_valid_state(machine: str, state: str) -> bool:
    return state in CANONICAL_STATES.get(machine, ())


def is_terminal(machine: str, state: str) -> bool:
    return state in TERMINAL_STATES.get(machine, frozenset())


def can_transition(machine: str, current: str, target: str) -> bool:
    """True when ``current -> target`` is allowed by the frozen table."""
    return target in TRANSITIONS.get(machine, {}).get(current, frozenset())


def all_states(machine: str) -> tuple[str, ...]:
    return CANONICAL_STATES[machine]


def all_transitions(machine: str) -> tuple[Transition, ...]:
    """Every legal transition of a machine (for exhaustive contract tests)."""
    return tuple(
        Transition(machine=machine, current=current, target=target)
        for current, targets in TRANSITIONS.get(machine, {}).items()
        for target in targets
    )


def validate_transition(machine: str, current: str, target: str) -> None:
    """Fail closed on illegal transitions / unknown states / terminal races.

    Raises ``StateTransitionError``. Terminal states have no out-edges, so a
    cancel/done/timeout race that reaches a terminal first makes the losing
    side fail here instead of inventing a state outside the table.
    """
    if not is_valid_state(machine, current):
        raise StateTransitionError(machine, current, target)
    if is_terminal(machine, current):
        raise StateTransitionError(machine, current, target)
    if not can_transition(machine, current, target):
        raise StateTransitionError(machine, current, target)


def run_cancel_allowed_from(state: str) -> bool:
    """Cancel commands may only be issued from pre-terminal run states."""
    return state in TRANSITIONS.get("run", {})
