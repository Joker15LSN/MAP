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
    """data = {"k": "a"*N} serializes to N+8 compact UTF-8 bytes.

    N=65527 -> 65535 (inline), N=65528 -> 65536 (inline, at the limit),
    N=65529 -> 65537 (rejected). The boundary must behave IDENTICALLY on
    construction, DB-row recovery and SSE outbound.
    """

    @pytest.mark.parametrize("size", [65535, 65536])
    def test_inline_boundary_all_paths(self, size: int) -> None:
        payload_size = size - 8
        envelope = _envelope(data={"k": "a" * payload_size})
        assert validate_payload_size(dict(envelope.data)) == size
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
        payload_size = 65537 - 8
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
