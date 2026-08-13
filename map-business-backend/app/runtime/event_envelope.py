"""Versioned event envelope (P0-CONTRACT-01, ADR-0002).

``event.v1`` = major 1 + minor increments. Unknown major / schema / event
type fail closed *before* write; minor bumps are forward compatible (unknown
fields preserved). Events are strictly ordered and unique per ``(run_id,
seq)``; SSE delivery is at-least-once with client dedupe on the same key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

UNKNOWN_EVENT_VERSION = "UNKNOWN_EVENT_VERSION"
UNKNOWN_EVENT_TYPE = "UNKNOWN_EVENT_TYPE"
EVENT_STALE_SEQ = "EVENT_STALE_SEQ"
ARTIFACT_PAYLOAD_TOO_LARGE = "ARTIFACT_PAYLOAD_TOO_LARGE"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

EVENT_SCHEMA_VERSION: Final[int] = 1
# acceptance-profile artifacts.inline_payload_max_bytes
INLINE_PAYLOAD_MAX_BYTES: Final[int] = 65536
PRESIGNED_URL_TTL_SECONDS: Final[int] = 300

# 冻结事件类型前缀（run.md §3）。未知前缀拒绝，已知前缀内的未定义类型拒绝。
_EVENT_TYPE_PREFIXES: Final[tuple[str, ...]] = (
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

_FROZEN_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "run.started",
        "run.completed",
        "run.failed",
        "run.cancelling",
        "run.cancelled",
        "run.timed_out",
        "step.started",
        "step.completed",
        "step.failed",
        "step.waiting_approval",
        "step.skipped",
        "step.cancelled",
        "attempt.started",
        "attempt.completed",
        "attempt.failed",
        "model.invocation_created",
        "model.invocation_sent",
        "model.invocation_succeeded",
        "model.invocation_failed",
        "model.invocation_unknown",
        "model.invocation_reconciled",
        "tool.invocation_created",
        "tool.invocation_completed",
        "tool.invocation_failed",
        "approval.created",
        "approval.approved",
        "approval.rejected",
        "approval.expired",
        "artifact.created",
        "checkpoint.written",
        "effect.planned",
        "effect.executing",
        "effect.succeeded",
        "effect.failed",
        "effect.uncertain",
        "effect.reconciling",
        "effect.reconciled",
        "effect.cancelled",
    }
)


class EventEnvelopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_event_type(event_type: str) -> None:
    if not any(event_type.startswith(prefix) for prefix in _EVENT_TYPE_PREFIXES):
        raise EventEnvelopeError(
            UNKNOWN_EVENT_TYPE, f"event type '{event_type}' has no known prefix"
        )
    if event_type not in _FROZEN_EVENT_TYPES:
        raise EventEnvelopeError(
            UNKNOWN_EVENT_TYPE, f"event type '{event_type}' is not defined"
        )


def validate_schema_version(version: int) -> None:
    if version != EVENT_SCHEMA_VERSION:
        raise EventEnvelopeError(
            UNKNOWN_EVENT_VERSION,
            f"unsupported schema major version {version}; supported: {EVENT_SCHEMA_VERSION}",
        )


def validate_payload_size(payload: Any, *, max_bytes: int = INLINE_PAYLOAD_MAX_BYTES) -> int:
    """Return the UTF-8 payload size; raise when it exceeds the inline limit.

    Oversized payloads must travel via ArtifactRef only
    (``ARTIFACT_PAYLOAD_TOO_LARGE``).
    """
    import json

    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > max_bytes:
        raise EventEnvelopeError(
            ARTIFACT_PAYLOAD_TOO_LARGE,
            f"payload is {size} bytes; inline limit is {max_bytes} bytes, "
            "use an ArtifactRef instead",
        )
    return size


@dataclass(frozen=True)
class ArtifactRef:
    """Manifest of an artifact stored in the private object store."""

    artifact_id: str
    workspace_id: str
    sha256: str
    size_bytes: int
    content_type: str
    policy_labels: tuple[str, ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "workspace_id": self.workspace_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if self.policy_labels:
            payload["policy_labels"] = list(self.policy_labels)
        return payload


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    schema_minor: int
    event_id: str
    run_id: str
    seq: int
    type: str
    occurred_at: str
    workspace_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_schema_version(self.schema_version)
        validate_event_type(self.type)
        if self.seq < 1:
            raise ValueError("seq must be >= 1")

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        seq: int,
        event_type: str,
        workspace_id: str,
        data: dict[str, Any] | None = None,
        schema_minor: int = 0,
        occurred_at: str | None = None,
    ) -> EventEnvelope:
        return cls(
            schema_version=EVENT_SCHEMA_VERSION,
            schema_minor=schema_minor,
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            seq=seq,
            type=event_type,
            occurred_at=occurred_at or datetime.now(UTC).isoformat(),
            workspace_id=workspace_id,
            data=data or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "schema_minor": self.schema_minor,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "workspace_id": self.workspace_id,
            "data": self.data,
        }

    def sse_frame(self) -> str:
        """At-least-once SSE frame; client dedupes on (run_id, seq)."""
        import json

        return (
            f"id: {self.seq}\nevent: {self.type}\n"
            f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"
        )
