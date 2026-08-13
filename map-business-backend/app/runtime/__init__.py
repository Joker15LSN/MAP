"""Canonical runtime contracts (P0-CONTRACT-01, ADR-0002).

State machines + versioned event envelope live here; PG durable tables
(runs/steps/events/checkpoints/...) land in P1-RUN-01.
"""

from .event_envelope import (
    ARTIFACT_PAYLOAD_TOO_LARGE,
    EVENT_SCHEMA_VERSION,
    EVENT_STALE_SEQ,
    IDEMPOTENCY_CONFLICT,
    UNKNOWN_EVENT_TYPE,
    UNKNOWN_EVENT_VERSION,
    ArtifactRef,
    EventEnvelope,
    EventEnvelopeError,
    validate_payload_size,
)
from .state_machine import (
    CANONICAL_STATES,
    RUN_TERMINAL_STATE,
    STATE_TRANSITION_VIOLATION,
    EffectState,
    EvidenceState,
    ModelInvocationState,
    RunState,
    StateTransitionError,
    StepState,
    all_states,
    all_transitions,
    can_transition,
    is_terminal,
    is_valid_state,
    run_cancel_allowed_from,
    validate_transition,
)

__all__ = [
    "ARTIFACT_PAYLOAD_TOO_LARGE",
    "CANONICAL_STATES",
    "EVENT_SCHEMA_VERSION",
    "EVENT_STALE_SEQ",
    "IDEMPOTENCY_CONFLICT",
    "RUN_TERMINAL_STATE",
    "STATE_TRANSITION_VIOLATION",
    "UNKNOWN_EVENT_TYPE",
    "UNKNOWN_EVENT_VERSION",
    "ArtifactRef",
    "EffectState",
    "EventEnvelope",
    "EventEnvelopeError",
    "EvidenceState",
    "ModelInvocationState",
    "RunState",
    "StateTransitionError",
    "StepState",
    "all_states",
    "all_transitions",
    "can_transition",
    "is_terminal",
    "is_valid_state",
    "run_cancel_allowed_from",
    "validate_payload_size",
    "validate_transition",
]
