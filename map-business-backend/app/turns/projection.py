"""Pure turn projection from canonical run events (Step 4 / PR-F1).

BFF-side counterpart of the frontend ``runProjection.ts`` rules. The
projection is deliberately a pure function over :class:`EventEnvelope`
values so it can be verified without a database or a worker:

- events are deduplicated by ``(run_id, seq)`` (at-least-once SSE);
- ``message.delta`` appends content increments;
- ``step.completed`` carries the authoritative full text;
- the FIRST terminal ``run.*`` event renders exactly once; any later
  terminal event (e.g. a stop/done race loser) is dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..runtime.event_envelope import EventEnvelope
from ..runtime.state_machine import RunState

_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.TIMED_OUT,
    }
)


@dataclass(frozen=True)
class TurnProjection:
    """Frozen projection result for one turn (run)."""

    run_id: str
    content: str
    terminal_status: str | None
    terminal_seen: bool
    last_seq: int


def project_turn_events(events: Iterable[EventEnvelope]) -> TurnProjection:
    """Fold a run's canonical events into the user-visible turn state."""
    run_id: str | None = None
    seen_seq: set[int] = set()
    content = ""
    step_completed_seen = False
    terminal_status: str | None = None
    terminal_seen = False
    last_seq = 0

    for envelope in events:
        if run_id is None:
            run_id = envelope.run_id
        if envelope.seq in seen_seq:
            continue
        seen_seq.add(envelope.seq)
        last_seq = max(last_seq, envelope.seq)

        if envelope.type == "message.delta":
            delta = envelope.data.get("content")
            if isinstance(delta, str) and not step_completed_seen:
                content += delta
        elif envelope.type == "step.completed":
            full_text = envelope.data.get("content")
            if isinstance(full_text, str):
                content = full_text
                step_completed_seen = True
        elif envelope.type.startswith("run."):
            status = envelope.type.removeprefix("run.")
            if status in _TERMINAL_RUN_STATUSES and not terminal_seen:
                terminal_status = status
                terminal_seen = True

    return TurnProjection(
        run_id=run_id or "",
        content=content,
        terminal_status=terminal_status,
        terminal_seen=terminal_seen,
        last_seq=last_seq,
    )
