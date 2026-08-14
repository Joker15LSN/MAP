"""Versioned event envelope (P0-CONTRACT-01, ADR-0002, review R-04).

"event.v1" = major 1 + minor increments. Unknown major / schema / event
type fail closed *before* write; minor bumps are forward compatible (unknown
fields preserved, never dropped). Events are strictly ordered and unique
per (run_id, seq); SSE delivery is at-least-once with client dedupe on the
same key.

R-04 hardening: the 64KiB inline limit and JSON serializability are enforced
IN the object constructor (EventEnvelope.__post_init__), on deserialization
(from_dict/from_json, the database-recovery entry) and on every canonical
serialization (to_json/sse_frame, the DB-write and SSE-outbound entry).
NaN/Infinity/non-serializable values are rejected with a typed error;
oversized payloads must travel via a validated ArtifactRef.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

UNKNOWN_EVENT_VERSION = "UNKNOWN_EVENT_VERSION"
UNKNOWN_EVENT_TYPE = "UNKNOWN_EVENT_TYPE"
EVENT_STALE_SEQ = "EVENT_STALE_SEQ"
ARTIFACT_PAYLOAD_TOO_LARGE = "ARTIFACT_PAYLOAD_TOO_LARGE"
PAYLOAD_NOT_SERIALIZABLE = "PAYLOAD_NOT_SERIALIZABLE"
ARTIFACT_REF_INVALID = "ARTIFACT_REF_INVALID"
EVENT_ENVELOPE_INVALID = "EVENT_ENVELOPE_INVALID"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

EVENT_SCHEMA_VERSION: Final[int] = 1
# acceptance-profile artifacts.inline_payload_max_bytes
INLINE_PAYLOAD_MAX_BYTES: Final[int] = 65536
PRESIGNED_URL_TTL_SECONDS: Final[int] = 300

# Frozen event type prefixes (run.md section 3). Unknown prefixes reject,
# and undefined types within a known prefix reject.
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

_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_TYPE = re.compile(r"^[\w!#$&^_.+~-]+/[\w!#$&^_.+~-]+$")
_UUID_STR = "uuid-string"


class EventEnvelopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _require_uuid(value: str, field_name: str) -> None:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise EventEnvelopeError(
            EVENT_ENVELOPE_INVALID,
            f"{field_name} must be a UUID string, got {value!r}",
        ) from exc


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


def _canonical_json_bytes(payload: Any) -> bytes:
    """Canonical JSON UTF-8 encoding; rejects NaN/Infinity/non-JSON values.

    R-04: json.dumps defaults would silently emit NaN/Infinity (invalid
    JSON) and TypeErrors for non-serializable objects; both must fail with
    the typed PAYLOAD_NOT_SERIALIZABLE error instead of reaching storage.
    """
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EventEnvelopeError(
            PAYLOAD_NOT_SERIALIZABLE,
            f"payload is not canonical JSON serializable: {exc}",
        ) from exc
    return text.encode("utf-8")


def validate_payload_size(payload: Any, *, max_bytes: int = INLINE_PAYLOAD_MAX_BYTES) -> int:
    """Return the UTF-8 payload size; raise when it exceeds the inline limit.

    Oversized payloads must travel via ArtifactRef only
    (ARTIFACT_PAYLOAD_TOO_LARGE). Non-serializable payloads raise
    PAYLOAD_NOT_SERIALIZABLE before any size accounting.
    """
    size = len(_canonical_json_bytes(payload))
    if size > max_bytes:
        raise EventEnvelopeError(
            ARTIFACT_PAYLOAD_TOO_LARGE,
            f"payload is {size} bytes; inline limit is {max_bytes} bytes, "
            "use an ArtifactRef instead",
        )
    return size


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise EventEnvelopeError(
            ARTIFACT_REF_INVALID,
            f"{field_name} must be an ISO-8601 timestamp, got {value!r}",
        ) from exc
    if parsed.tzinfo is None:
        raise EventEnvelopeError(
            ARTIFACT_REF_INVALID, f"{field_name} must carry a timezone offset"
        )
    return parsed


@dataclass(frozen=True)
class ArtifactRef:
    """Manifest of an artifact stored in the private object store (run.md)."""

    artifact_id: str
    workspace_id: str
    sha256: str
    size_bytes: int
    content_type: str
    policy_labels: tuple[str, ...]
    expires_at: str
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def __post_init__(self) -> None:
        for field_name, value in (
            ("artifact_id", self.artifact_id),
            ("workspace_id", self.workspace_id),
        ):
            try:
                uuid.UUID(str(value))
            except (ValueError, AttributeError, TypeError) as exc:
                raise EventEnvelopeError(
                    ARTIFACT_REF_INVALID,
                    f"{field_name} must be a UUID string",
                ) from exc
        if not _SHA256.fullmatch(self.sha256):
            raise EventEnvelopeError(
                ARTIFACT_REF_INVALID,
                "sha256 must be exactly 64 lowercase hex characters",
            )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise EventEnvelopeError(
                ARTIFACT_REF_INVALID, "size_bytes must be an integer"
            )
        if self.size_bytes < 0:
            raise EventEnvelopeError(
                ARTIFACT_REF_INVALID, "size_bytes must be >= 0"
            )
        if not _CONTENT_TYPE.fullmatch(self.content_type):
            raise EventEnvelopeError(
                ARTIFACT_REF_INVALID,
                f"content_type must be a media type, got {self.content_type!r}",
            )
        labels = tuple(self.policy_labels)
        if not labels or any(
            not isinstance(label, str) or not label.strip() for label in labels
        ):
            raise EventEnvelopeError(
                ARTIFACT_REF_INVALID,
                "policy_labels must contain at least one non-empty label",
            )
        created = _parse_timestamp(self.created_at, "created_at")
        expires = _parse_timestamp(self.expires_at, "expires_at")
        if expires <= created:
            raise EventEnvelopeError(
                ARTIFACT_REF_INVALID,
                "expires_at must be strictly later than created_at",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "workspace_id": self.workspace_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "policy_labels": list(self.policy_labels),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


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
    # Unknown top-level fields of a newer minor version, preserved verbatim
    # (forward compatibility). Never participates in validation beyond
    # serializability; merged back into to_dict so nothing is dropped.
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # R-04: the full validation runs at construction time, so a 70KB
        # payload (or a non-JSON payload) can never exist as an object.
        validate_schema_version(self.schema_version)
        validate_event_type(self.type)
        if self.seq < 1:
            raise EventEnvelopeError(EVENT_ENVELOPE_INVALID, "seq must be >= 1")
        for field_name, value in (
            ("event_id", self.event_id),
            ("run_id", self.run_id),
            ("occurred_at", self.occurred_at),
            ("workspace_id", self.workspace_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise EventEnvelopeError(
                    EVENT_ENVELOPE_INVALID, f"{field_name} must be a non-empty string"
                )
        _require_uuid(self.run_id, "run_id")
        _require_uuid(self.workspace_id, "workspace_id")
        validate_payload_size(self.data)
        validate_payload_size(self.extra_fields)

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
        payload: dict[str, Any] = {
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
        payload.update(self.extra_fields)
        return payload

    def to_json(self) -> str:
        """Canonical JSON used for DB writes and SSE frames (validated).

        The shared serializer is the same code path used by the size check
        (validate_payload_size), so DB writes and SSE frames can never
        bypass construction-time validation.
        """
        _canonical_json_bytes(self.to_dict())
        return json.dumps(
            self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )

    def sse_frame(self) -> str:
        """At-least-once SSE frame; client dedupes on (run_id, seq)."""
        return (
            f"id: {self.seq}\nevent: {self.type}\n"
            f"data: {self.to_json()}\n\n"
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EventEnvelope:
        """Deserialize (the database-recovery entry) with full validation.

        Unknown major versions reject; unknown minor fields are preserved
        in extra_fields (forward compatible, never dropped).
        """
        if not isinstance(payload, dict):
            raise EventEnvelopeError(
                EVENT_ENVELOPE_INVALID, "envelope payload must be a JSON object"
            )
        version = payload.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise EventEnvelopeError(
                EVENT_ENVELOPE_INVALID, "schema_version must be an integer"
            )
        validate_schema_version(version)

        required = (
            "schema_minor",
            "event_id",
            "run_id",
            "seq",
            "type",
            "occurred_at",
            "workspace_id",
        )
        for key in required:
            if key not in payload:
                raise EventEnvelopeError(
                    EVENT_ENVELOPE_INVALID, f"missing required field '{key}'"
                )

        known = {"schema_version", "schema_minor", "event_id", "run_id", "seq",
                 "type", "occurred_at", "workspace_id", "data"}
        extra_fields = {k: v for k, v in payload.items() if k not in known}
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise EventEnvelopeError(
                EVENT_ENVELOPE_INVALID, "data must be a JSON object"
            )

        envelope = cls(
            schema_version=payload["schema_version"],
            schema_minor=payload["schema_minor"],
            event_id=payload["event_id"],
            run_id=payload["run_id"],
            seq=payload["seq"],
            type=payload["type"],
            occurred_at=payload["occurred_at"],
            workspace_id=payload["workspace_id"],
            data=data,
            extra_fields=extra_fields,
        )
        # extra_fields must survive canonical serialization.
        validate_payload_size(extra_fields)
        return envelope

    @classmethod
    def from_json(cls, raw: str) -> EventEnvelope:
        """Parse a canonical JSON envelope string (SSE replay / DB row)."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EventEnvelopeError(
                EVENT_ENVELOPE_INVALID, f"envelope is not valid JSON: {exc}"
            ) from exc
        return cls.from_dict(payload)
