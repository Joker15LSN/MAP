"""S2-02: envelope hardening - mutation/reserved-field/type bypasses and the
65,535/65,536/65,537 boundary across construction, DB-row recovery and SSE
outbound, plus the production run-event codec (run_event_stream).

The review planted four bypasses:

1. post-construction ``envelope.data["x"] = "a" * 70000`` still serialized;
2. ``extra_fields`` could shadow canonical fields (schema_version=2);
3. ``seq=True`` / negative ``schema_minor`` / invalid ``occurred_at``
   constructed fine;
4. the 64KiB boundary was not re-checked on serialization.

All four must now fail with stable typed errors, and the boundary must
behave identically on the API/DB-recovery/SSE paths.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.runtime.event_envelope import (
    ARTIFACT_PAYLOAD_TOO_LARGE,
    EVENT_ENVELOPE_INVALID,
    PAYLOAD_NOT_SERIALIZABLE,
    EventEnvelope,
    EventEnvelopeError,
    validate_payload_size,
)
from app.services.run_event_stream import (
    envelope_to_row,
    parse_run_event_data,
    row_to_envelope,
)

RUN_ID = str(uuid.uuid4())
WORKSPACE = str(uuid.uuid4())
EVENT_ID = str(uuid.uuid4())


def _envelope(**overrides) -> EventEnvelope:
    kwargs = {
        "schema_version": 1,
        "schema_minor": 0,
        "event_id": EVENT_ID,
        "run_id": RUN_ID,
        "seq": 1,
        "type": "run.started",
        "occurred_at": "2026-08-13T12:00:00+00:00",
        "workspace_id": WORKSPACE,
        "data": {"answer": "hi"},
    }
    kwargs.update(overrides)
    return EventEnvelope(**kwargs)


class TestMutationBypass:
    def test_top_level_data_mutation_raises(self) -> None:
        envelope = _envelope()
        with pytest.raises(TypeError):
            envelope.data["x"] = "a" * 70000  # type: ignore[index]

    def test_nested_data_mutation_raises(self) -> None:
        envelope = _envelope(data={"nested": {"k": "v"}})
        with pytest.raises(TypeError):
            envelope.data["nested"]["k"] = "a" * 70000  # type: ignore[index]

    def test_extra_fields_mutation_raises(self) -> None:
        envelope = _envelope(extra_fields={"minor_field": 1})
        with pytest.raises(TypeError):
            envelope.extra_fields["minor_field"] = 2  # type: ignore[index]

    def test_serialization_is_unchanged_after_attempted_mutation(self) -> None:
        envelope = _envelope()
        before = envelope.to_json()
        with pytest.raises(TypeError):
            envelope.data["x"] = "a" * 70000  # type: ignore[index]
        assert envelope.to_json() == before
        assert len(envelope.to_json()) < 70000


class TestReservedFieldBypass:
    @pytest.mark.parametrize(
        "reserved",
        ["schema_version", "schema_minor", "event_id", "run_id", "seq",
         "type", "occurred_at", "workspace_id", "data"],
    )
    def test_extra_fields_cannot_shadow_canonical(self, reserved: str) -> None:
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(extra_fields={reserved: 999})
        assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    def test_legit_extra_fields_still_roundtrip(self) -> None:
        envelope = _envelope(extra_fields={"future_minor_flag": True, "meta": {"a": 1}})
        payload = json.loads(envelope.to_json())
        assert payload["future_minor_flag"] is True
        assert payload["schema_version"] == 1  # canonical fields intact
        restored = EventEnvelope.from_json(envelope.to_json())
        assert dict(restored.extra_fields) == {"future_minor_flag": True, "meta": {"a": 1}}


class TestTypeBypass:
    def test_bool_schema_version_rejected(self) -> None:
        """S3-06: True == 1 in Python - a bool schema_version must fail."""
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(schema_version=True)
        assert exc_info.value.code == "UNKNOWN_EVENT_VERSION"

    def test_falsey_list_data_rejected_by_build(self) -> None:
        """S3-06: build() coerces ONLY None to {}; a falsey list reaches the
        type check and fails instead of becoming an empty dict."""
        with pytest.raises(EventEnvelopeError) as exc_info:
            EventEnvelope.build(
                run_id=RUN_ID,
                seq=1,
                event_type="run.started",
                workspace_id=WORKSPACE,
                data=[],  # type: ignore[arg-type]
            )
        assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    def test_none_data_still_defaults_to_empty_dict(self) -> None:
        envelope = EventEnvelope.build(
            run_id=RUN_ID,
            seq=1,
            event_type="run.started",
            workspace_id=WORKSPACE,
            data=None,
        )
        assert dict(envelope.data) == {}

    def test_seq_true_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(seq=True)
        assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    def test_seq_zero_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError):
            _envelope(seq=0)

    def test_negative_schema_minor_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError):
            _envelope(schema_minor=-1)

    def test_bool_schema_minor_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError):
            _envelope(schema_minor=False)

    def test_naive_occurred_at_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(occurred_at="2026-08-13T12:00:00")  # no offset
        assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    def test_garbage_occurred_at_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError):
            _envelope(occurred_at="not-a-timestamp")

    def test_non_uuid_event_id_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError):
            _envelope(event_id="hello-world")

    def test_non_dict_data_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError):
            _envelope(data=[1, 2, 3])  # type: ignore[arg-type]

    def test_non_json_data_value_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(data={"blob": b"\x00\x01"})  # type: ignore[dict-item]
        assert exc_info.value.code == PAYLOAD_NOT_SERIALIZABLE

    def test_nan_data_rejected(self) -> None:
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(data={"x": float("nan")})
        assert exc_info.value.code == PAYLOAD_NOT_SERIALIZABLE


class Test64kBoundaryAcrossPaths:
    """S3-06: the FROZEN budget is the WHOLE standardized envelope
    (data + extra_fields + metadata, compact JSON).

    to_json must be 65535/65536 bytes at the inline limit and 65537 must be
    rejected - identically on construction, DB-row recovery and SSE
    outbound. Splitting ~64KiB across data AND extra_fields cannot sneak
    through as a ~130KiB event.
    """

    def _total(self, data: dict, **overrides) -> int:
        return len(_envelope(data=data, **overrides).to_json().encode("utf-8"))

    @pytest.mark.parametrize("size", [65535, 65536])
    def test_inline_boundary_all_paths(self, size: int) -> None:
        base = self._total({})
        payload_size = size - base - 6  # {"k":"..."} adds 8 bytes; {} is 2
        envelope = _envelope(data={"k": "a" * payload_size})
        assert len(envelope.to_json().encode("utf-8")) == size
        assert validate_payload_size(envelope.to_dict()) == size
        # construction path passed; SSE outbound shares the same serializer
        frame = envelope.sse_frame()
        assert frame.startswith("id: 1\nevent: run.started\ndata: ")
        # DB-recovery path: row roundtrip preserves the envelope
        row = envelope_to_row(envelope)
        restored = row_to_envelope(row)
        assert restored.run_id == envelope.run_id
        assert restored.seq == envelope.seq
        assert len(restored.to_json()) == len(envelope.to_json())

    def test_oversized_rejected_on_all_paths(self) -> None:
        base = self._total({})
        payload_size = 65537 - base - 6
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(data={"k": "a" * payload_size})
        assert exc_info.value.code == ARTIFACT_PAYLOAD_TOO_LARGE

        # a hand-crafted oversized ROW must fail recovery with the same code
        # (a poisoned durable row bypasses construction entirely; only the
        # recovery path's validation can catch it)
        oversized_payload = json.dumps(
            {
                "schema_version": 1,
                "schema_minor": 0,
                "event_id": str(uuid.uuid4()),
                "run_id": RUN_ID,
                "seq": 2,
                "type": "run.started",
                "occurred_at": "2026-08-13T12:00:00+00:00",
                "workspace_id": WORKSPACE,
                "data": {"k": "a" * payload_size},
            }
        )
        poisoned_row = {
            "run_id": RUN_ID,
            "seq": 2,
            "event_id": str(uuid.uuid4()),
            "event_type": "run.started",
            "occurred_at": "2026-08-13T12:00:00+00:00",
            "workspace_id": WORKSPACE,
            "schema_version": 1,
            "schema_minor": 0,
            "payload_json": oversized_payload,
        }
        with pytest.raises(EventEnvelopeError) as exc_info:
            row_to_envelope(poisoned_row)
        assert exc_info.value.code == ARTIFACT_PAYLOAD_TOO_LARGE

    def test_split_across_data_and_extra_fields_is_budgeted_together(self) -> None:
        """S3-06: ~64KiB of data PLUS ~64KiB of extra_fields cannot sneak
        through as a ~130KiB event - the whole envelope is budgeted."""
        base = self._total({})
        half = (65536 - base - 12) // 2
        with pytest.raises(EventEnvelopeError) as exc_info:
            _envelope(
                data={"k": "a" * half},
                extra_fields={"future_field": "b" * half},
            )
        assert exc_info.value.code == ARTIFACT_PAYLOAD_TOO_LARGE

class TestRunEventCodec:
    def test_parse_valid_run_event(self) -> None:
        envelope = _envelope(type="step.started")
        data = json.loads(envelope.to_json())
        parsed = parse_run_event_data("step.started", data)
        assert parsed.type == "step.started"

    def test_frame_event_type_mismatch_rejected(self) -> None:
        envelope = _envelope(type="step.started")
        data = json.loads(envelope.to_json())
        with pytest.raises(EventEnvelopeError) as exc_info:
            parse_run_event_data("step.succeeded", data)
        assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    def test_row_tamper_detected(self) -> None:
        envelope = _envelope()
        row = envelope_to_row(envelope)
        row["seq"] = 999  # tamper with the denormalized column
        with pytest.raises(EventEnvelopeError) as exc_info:
            row_to_envelope(row)
        assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    def test_unknown_major_version_row_rejected(self) -> None:
        envelope = _envelope()
        row = envelope_to_row(envelope)
        payload = json.loads(row["payload_json"])
        payload["schema_version"] = 2
        row["payload_json"] = json.dumps(payload)
        row["schema_version"] = 2
        with pytest.raises(EventEnvelopeError) as exc_info:
            row_to_envelope(row)
        assert exc_info.value.code == "UNKNOWN_EVENT_VERSION"
