"""S2-02: production run-event codec wired into the BFF SSE path.

The canonical runtime contracts (EventEnvelope / state machine / typed
errors, app/runtime/) previously had callers only inside tests. This
module is their PRODUCTION entry point:

- ``parse_run_event_data``: inbound SSE frame data -> validated
  :class:`EventEnvelope` (used by conversation_service before forwarding
  run events on the existing SSE channel);
- ``encode_sse_frame``: outbound SSE frame via ``envelope.sse_frame()`` -
  the shared serializer that re-runs the full 64KiB/serializability
  validation on every write;
- ``envelope_to_row`` / ``row_to_envelope``: the PG durable-row shape for
  P1-RUN-01 (runs/steps/events tables). The recovery path re-validates the
  envelope AND cross-checks the row columns against it, so a tampered row
  fails with a stable typed error.

Run events are identified by the frozen event-type prefixes from
SPEC/contracts/run.md; frames outside that set keep flowing through the
existing conversation dispatch untouched.
"""

from __future__ import annotations

from typing import Any

from ..runtime.event_envelope import (
    EVENT_ENVELOPE_INVALID,
    EventEnvelope,
    EventEnvelopeError,
)

RUN_EVENT_PREFIXES: tuple[str, ...] = (
    "run.",
    "step.",
    "attempt.",
    "model.",
    "tool.",
    "approval.",
    "artifact.",
    "checkpoint.",
    "effect.",
)

# Durable-row columns (P1-RUN-01). The full envelope JSON is stored in
# payload_json; the denormalized columns serve the (run_id, seq) unique
# index and point-in-time replay queries.
ROW_COLUMNS = (
    "run_id",
    "seq",
    "event_id",
    "event_type",
    "occurred_at",
    "workspace_id",
    "schema_version",
    "schema_minor",
    "payload_json",
)


def is_run_event_frame(event_name: str) -> bool:
    return any(event_name.startswith(prefix) for prefix in RUN_EVENT_PREFIXES)


def parse_run_event_data(event_name: str, data: dict[str, Any]) -> EventEnvelope:
    """Validate an inbound run-event frame against the canonical contract.

    Raises :class:`EventEnvelopeError` with a stable code on any violation:
    unknown major version, unknown event type, reserved-field shadowing,
    malformed ids/timestamps or an oversized payload.
    """
    envelope = EventEnvelope.from_dict(data)
    if envelope.type != event_name:
        raise EventEnvelopeError(
            EVENT_ENVELOPE_INVALID,
            f"SSE frame event {event_name!r} does not match envelope type "
            f"{envelope.type!r}",
        )
    return envelope


def encode_sse_frame(envelope: EventEnvelope) -> str:
    """Outbound SSE frame (id/event/data) through the shared validated
    serializer - the same 64KiB/serializability boundary as the DB write."""
    return envelope.sse_frame()


def envelope_to_row(envelope: EventEnvelope) -> dict[str, Any]:
    """Project an envelope into the durable PG row shape (P1-RUN-01)."""
    payload = envelope.to_json()  # full validation re-runs here
    return {
        "run_id": envelope.run_id,
        "seq": envelope.seq,
        "event_id": envelope.event_id,
        "event_type": envelope.type,
        "occurred_at": envelope.occurred_at,
        "workspace_id": envelope.workspace_id,
        "schema_version": envelope.schema_version,
        "schema_minor": envelope.schema_minor,
        "payload_json": payload,
    }


def row_to_envelope(row: dict[str, Any]) -> EventEnvelope:
    """Recover an envelope from a durable row (the DB-recovery entry).

    The envelope is fully re-validated from payload_json AND every
    denormalized column is cross-checked against it, so a row whose
    columns drifted from its payload fails with EVENT_ENVELOPE_INVALID.
    """
    if not isinstance(row, dict):
        raise EventEnvelopeError(EVENT_ENVELOPE_INVALID, "row must be an object")
    payload_json = row.get("payload_json")
    if not isinstance(payload_json, str) or not payload_json:
        raise EventEnvelopeError(EVENT_ENVELOPE_INVALID, "row lacks payload_json")
    envelope = EventEnvelope.from_json(payload_json)
    expected = {
        "run_id": envelope.run_id,
        "seq": envelope.seq,
        "event_id": envelope.event_id,
        "event_type": envelope.type,
        "occurred_at": envelope.occurred_at,
        "workspace_id": envelope.workspace_id,
        "schema_version": envelope.schema_version,
        "schema_minor": envelope.schema_minor,
    }
    for column, expected_value in expected.items():
        if row.get(column) != expected_value:
            raise EventEnvelopeError(
                EVENT_ENVELOPE_INVALID,
                f"row column {column!r} ({row.get(column)!r}) does not match "
                f"the envelope payload ({expected_value!r})",
            )
    return envelope
