"""P0-CONTRACT-01 contract tests (AC-CONTRACT-01/03/05/07/08, review R-04/R-05/R-09).

- AC-CONTRACT-01: exhaustive transition-table coverage for all five machines
  (legal transitions pass, illegal / terminal out-edges / unknown states
  fail) with SPECIFIC exceptions only - pytest.raises(Exception) is banned.
- R-05: run_cancel_allowed_from uses the explicit allow-list; every run
  state (including the non-canonical "cancel_pending") is covered.
- AC-CONTRACT-03: SSE frame shape + client dedupe key semantics.
- AC-CONTRACT-05: REAL EventEnvelope 64KiB boundary at 65535/65536/65537
  bytes including multibyte characters, plus non-JSON value rejection.
- AC-CONTRACT-07: typed error codes verified against the real registry and
  the real HTTP/SSE mapping (app.runtime.error_mapping).
- AC-CONTRACT-08: unknown major/event-type fail closed; minor forward
  compatibility through the REAL parser (from_dict/from_json).
"""

from __future__ import annotations

import json

import pytest

from app.runtime import (
    ARTIFACT_PAYLOAD_TOO_LARGE,
    ARTIFACT_REF_INVALID,
    EVENT_ENVELOPE_INVALID,
    EVENT_SCHEMA_VERSION,
    IDEMPOTENCY_CONFLICT,
    PAYLOAD_NOT_SERIALIZABLE,
    RUN_TERMINAL_STATE,
    STATE_TRANSITION_VIOLATION,
    UNKNOWN_EVENT_TYPE,
    UNKNOWN_EVENT_VERSION,
    ArtifactRef,
    EventEnvelope,
    EventEnvelopeError,
    StateTransitionError,
    all_states,
    all_transitions,
    can_transition,
    is_terminal,
    is_valid_state,
    run_cancel_allowed_from,
    validate_payload_size,
    validate_transition,
)

MACHINES = ("run", "step", "effect", "model_invocation", "evidence")

RUN_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"


def test_machine_enum_values_match_acceptance_profile() -> None:
    """Anchor: CANONICAL_STATES must equal TODO/acceptance-profile.yaml.

    The profile file is parsed directly (not a hand-copied mirror) so any
    drift between the normative profile and the implementation fails CI.
    """
    from pathlib import Path

    import yaml

    from app.runtime.state_machine import CANONICAL_STATES

    repo_root = Path(__file__).resolve().parents[3]
    profile_path = repo_root / "TODO" / "acceptance-profile.yaml"
    assert profile_path.is_file(), profile_path
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    profile_canonical = profile["canonical_states"]
    # legacy_projection is a mapping block, not a state machine
    profile_machines = {k: v for k, v in profile_canonical.items() if k in MACHINES}
    assert set(profile_machines) == set(CANONICAL_STATES)
    for machine in MACHINES:
        assert tuple(CANONICAL_STATES[machine]) == tuple(
            profile_machines[machine]
        ), machine


@pytest.mark.parametrize("machine", MACHINES)
def test_every_legal_transition_passes(machine: str) -> None:
    transitions = all_transitions(machine)
    assert transitions, f"{machine} must have at least one legal transition"
    for t in transitions:
        assert can_transition(machine, t.current, t.target)
        validate_transition(machine, t.current, t.target)  # must not raise


@pytest.mark.parametrize("machine", MACHINES)
def test_every_illegal_transition_fails_closed(machine: str) -> None:
    states = all_states(machine)
    legal = {(t.current, t.target) for t in all_transitions(machine)}
    illegal_count = 0
    for current in states:
        for target in states:
            if (current, target) in legal:
                continue
            if current == target:
                continue  # self-loop is not a transition
            assert not can_transition(machine, current, target), (machine, current, target)
            with pytest.raises(StateTransitionError) as exc_info:
                validate_transition(machine, current, target)
            assert str(exc_info.value).startswith(STATE_TRANSITION_VIOLATION), (
                machine,
                current,
                target,
            )
            assert exc_info.value.machine == machine
            assert exc_info.value.current == current
            assert exc_info.value.target == target
            illegal_count += 1
    assert illegal_count > 0


@pytest.mark.parametrize("machine", MACHINES)
def test_terminal_states_have_no_out_edges(machine: str) -> None:
    states = all_states(machine)
    for state in states:
        if not is_terminal(machine, state):
            continue
        for target in states:
            if target == state:
                continue
            assert not can_transition(machine, state, target), (
                f"{machine} terminal '{state}' must have no out-edge to '{target}'"
            )
            with pytest.raises(StateTransitionError):
                validate_transition(machine, state, target)


@pytest.mark.parametrize("machine", MACHINES)
def test_unknown_states_fail_closed(machine: str) -> None:
    assert not is_valid_state(machine, "totally_unknown_state")
    with pytest.raises(StateTransitionError) as exc_info:
        validate_transition(machine, "totally_unknown_state", all_states(machine)[0])
    assert str(exc_info.value).startswith(STATE_TRANSITION_VIOLATION)
    assert exc_info.value.current == "totally_unknown_state"


# --- R-05: cancel predicate is an explicit allow-list ------------------------

ALL_RUN_STATES = [
    "queued",
    "running",
    "paused",
    "cancelling",
    "cancel_pending",  # not a canonical run state; must never be allowed
    "timed_out",
    "failed",
    "completed",
    "cancelled",
]

CANCEL_ALLOWED = {"queued", "running", "paused"}


@pytest.mark.parametrize("state", ALL_RUN_STATES)
def test_run_cancel_allowed_from_exhaustive(state: str) -> None:
    expected = state in CANCEL_ALLOWED
    assert run_cancel_allowed_from(state) is expected, state


def test_run_cancel_race_converges_to_allowed_terminal() -> None:
    """cancel/done/timeout race: loser must fail closed, never invent states."""
    # done wins first -> cancel attempt on terminal fails closed
    with pytest.raises(StateTransitionError) as exc_info:
        validate_transition("run", "completed", "cancelling")
    assert str(exc_info.value).startswith(STATE_TRANSITION_VIOLATION)
    # cancel wins first -> cancelling converges to cancelled only
    validate_transition("run", "running", "cancelling")
    validate_transition("run", "cancelling", "cancelled")
    with pytest.raises(StateTransitionError):
        validate_transition("run", "cancelling", "completed")
    # cancelling must NOT be a valid cancel entry point (R-05)
    assert not run_cancel_allowed_from("cancelling")


# --- AC-CONTRACT-03: SSE frame and dedupe key --------------------------------

def test_event_envelope_sse_frame_and_dedupe_key() -> None:
    env = EventEnvelope.build(
        run_id=RUN_ID,
        seq=7,
        event_type="run.started",
        workspace_id=WORKSPACE_ID,
        data={"hello": "world"},
    )
    frame = env.sse_frame()
    lines = frame.splitlines()
    assert lines[0] == "id: 7"
    assert lines[1] == "event: run.started"
    payload = json.loads(lines[2][len("data: "):])
    # client dedupe key = (run_id, seq)
    assert (payload["run_id"], payload["seq"]) == (RUN_ID, 7)
    # replayed frame keeps the same key so clients can dedupe terminal events
    replay = EventEnvelope.build(
        run_id=RUN_ID,
        seq=7,
        event_type="run.started",
        workspace_id=WORKSPACE_ID,
    )
    assert (replay.run_id, replay.seq) == (env.run_id, env.seq)


def test_event_envelope_seq_must_be_positive() -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope.build(
            run_id=RUN_ID,
            seq=0,
            event_type="run.started",
            workspace_id=WORKSPACE_ID,
        )
    assert exc_info.value.code == EVENT_ENVELOPE_INVALID


# --- AC-CONTRACT-05: REAL envelope 64KiB boundary ----------------------------
# S3-06: the frozen budget applies to the WHOLE standardized envelope
# (data + extra_fields + metadata <= 64 KiB), identical on construction,
# DB-row recovery and SSE outbound.

def _build_with_data(data: dict) -> EventEnvelope:
    return EventEnvelope.build(
        run_id=RUN_ID,
        seq=1,
        event_type="run.started",
        workspace_id=WORKSPACE_ID,
        data=data,
    )


def _envelope_total_bytes(data: dict) -> int:
    return len(_build_with_data(data).to_json().encode("utf-8"))


@pytest.mark.parametrize(
    ("total_bytes", "allowed"),
    [
        (65535, True),  # whole envelope == 65535 -> inline
        (65536, True),  # 65536 == the frozen limit, allowed
        (65537, False),  # 65537 -> ARTIFACT_PAYLOAD_TOO_LARGE
    ],
)
def test_real_envelope_64k_boundary(total_bytes: int, allowed: bool) -> None:
    # data = {"x": "a"*N} serializes compactly as 8 + N bytes; the empty
    # envelope (data={}) is base_bytes, so the offset from data={} to
    # data={"x":""} is 6 bytes.
    base_bytes = _envelope_total_bytes({})
    extra = total_bytes - base_bytes - 6
    data = {"x": "a" * extra}
    if allowed:
        envelope = _build_with_data(data)  # must not raise
        assert len(envelope.to_json().encode("utf-8")) == total_bytes
        assert validate_payload_size(envelope.to_dict()) == total_bytes
        # serialization (DB write / SSE outbound) succeeds too
        assert len(envelope.to_json()) > 0
    else:
        with pytest.raises(EventEnvelopeError) as exc_info:
            _build_with_data(data)
        assert exc_info.value.code == ARTIFACT_PAYLOAD_TOO_LARGE


def test_real_envelope_multibyte_boundary() -> None:
    # Byte count, not character count: "界" is 3 UTF-8 bytes. The whole
    # envelope must stay within the frozen budget.
    base_bytes = _envelope_total_bytes({})
    assert _envelope_total_bytes({"x": "界" * 100}) < 65536
    # find the exact cut: total = base + 6 + 3n
    max_chars = (65536 - base_bytes - 6) // 3
    envelope = _build_with_data({"x": "界" * max_chars})
    assert len(envelope.to_json().encode("utf-8")) <= 65536
    with pytest.raises(EventEnvelopeError) as exc_info:
        _build_with_data({"x": "界" * (max_chars + 1)})  # > 65536 -> too large
    assert exc_info.value.code == ARTIFACT_PAYLOAD_TOO_LARGE


@pytest.mark.parametrize(
    "bad_value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        {"obj": object()},
        {"set": {1, 2, 3}},
        {"bytes": b"raw"},
    ],
)
def test_real_envelope_rejects_non_json_values(bad_value) -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        _build_with_data({"payload": bad_value})
    assert exc_info.value.code == PAYLOAD_NOT_SERIALIZABLE


def test_payload_over_limit_requires_artifact_ref() -> None:
    """Oversized payload must travel via ArtifactRef manifest, not inline."""
    ref = ArtifactRef(
        artifact_id="33333333-3333-3333-3333-333333333333",
        workspace_id=WORKSPACE_ID,
        sha256="a" * 64,
        size_bytes=123456,
        content_type="application/octet-stream",
        policy_labels=("internal",),
        expires_at="2026-08-14T00:05:00+00:00",
        created_at="2026-08-14T00:00:00+00:00",
    )
    manifest = ref.to_dict()
    for required in (
        "artifact_id",
        "workspace_id",
        "sha256",
        "size_bytes",
        "content_type",
        "policy_labels",
        "created_at",
        "expires_at",
    ):
        assert required in manifest
    assert manifest["policy_labels"] == ["internal"]


# --- ArtifactRef per-field typed errors (R-04) -------------------------------

def _valid_ref(**overrides) -> dict:
    base = {
        "artifact_id": "33333333-3333-3333-3333-333333333333",
        "workspace_id": WORKSPACE_ID,
        "sha256": "a" * 64,
        "size_bytes": 10,
        "content_type": "text/plain",
        "policy_labels": ("internal",),
        "expires_at": "2026-08-14T00:05:00+00:00",
        "created_at": "2026-08-14T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "overrides",
    [
        {"artifact_id": ""},
        {"artifact_id": "not-a-uuid"},
        {"workspace_id": ""},
        {"workspace_id": "nope"},
        {"sha256": ""},
        {"sha256": "a" * 63},
        {"sha256": "A" * 64},
        {"sha256": "g" * 64},
        {"size_bytes": -1},
        {"size_bytes": 1.5},
        {"size_bytes": True},
        {"content_type": ""},
        {"content_type": "no-slash"},
        {"content_type": "bad//type"},
        {"policy_labels": ()},
        {"policy_labels": ("",)},
        {"policy_labels": ("ok", "  ")},
        {"created_at": "not-a-date"},
        {"expires_at": "not-a-date"},
        {"expires_at": "2026-08-14T00:00:00+00:00"},  # equals created_at
        {"expires_at": "2026-08-13T23:00:00+00:00"},  # before created_at
    ],
)
def test_artifact_ref_invalid_field_typed_error(overrides: dict) -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        ArtifactRef(**_valid_ref(**overrides))
    assert exc_info.value.code == ARTIFACT_REF_INVALID


# --- AC-CONTRACT-07/08: versions and typed errors ----------------------------

def test_unknown_event_version_fails_closed() -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope(
            schema_version=99,
            schema_minor=0,
            event_id="e",
            run_id=RUN_ID,
            seq=1,
            type="run.started",
            occurred_at="now",
            workspace_id=WORKSPACE_ID,
        )
    assert exc_info.value.code == UNKNOWN_EVENT_VERSION


def test_unknown_event_type_fails_closed() -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope.build(
            run_id=RUN_ID,
            seq=1,
            event_type="run.not_a_real_event",
            workspace_id=WORKSPACE_ID,
        )
    assert exc_info.value.code == UNKNOWN_EVENT_TYPE
    # unknown prefix also fails closed
    with pytest.raises(EventEnvelopeError) as exc_info2:
        EventEnvelope.build(
            run_id=RUN_ID, seq=1, event_type="mystery.explosion", workspace_id=WORKSPACE_ID
        )
    assert exc_info2.value.code == UNKNOWN_EVENT_TYPE


def test_minor_version_forward_compatible_through_real_parser() -> None:
    """A newer minor version parsed from JSON keeps unknown fields intact."""
    envelope = EventEnvelope.build(
        run_id=RUN_ID,
        seq=1,
        event_type="run.started",
        workspace_id=WORKSPACE_ID,
        schema_minor=7,
        data={"future_field": "kept"},
    )
    raw = envelope.to_json()
    parsed = EventEnvelope.from_json(raw)
    assert parsed.schema_version == EVENT_SCHEMA_VERSION
    assert parsed.schema_minor == 7
    assert parsed.data["future_field"] == "kept"
    # unknown TOP-LEVEL fields of a future minor are preserved too
    future_payload = json.loads(raw)
    future_payload["schema_minor"] = 8
    future_payload["future_top_field"] = "preserved"
    reparsed = EventEnvelope.from_dict(future_payload)
    assert reparsed.extra_fields["future_top_field"] == "preserved"
    assert reparsed.to_dict()["future_top_field"] == "preserved"


def test_from_json_rejects_malformed_and_missing_fields() -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope.from_json("{not json")
    assert exc_info.value.code == EVENT_ENVELOPE_INVALID

    valid = json.loads(
        EventEnvelope.build(
            run_id=RUN_ID,
            seq=1,
            event_type="run.started",
            workspace_id=WORKSPACE_ID,
        ).to_json()
    )
    del valid["workspace_id"]
    with pytest.raises(EventEnvelopeError) as exc_info2:
        EventEnvelope.from_dict(valid)
    assert exc_info2.value.code == EVENT_ENVELOPE_INVALID


def test_from_json_rejects_unknown_major() -> None:
    valid = json.loads(
        EventEnvelope.build(
            run_id=RUN_ID,
            seq=1,
            event_type="run.started",
            workspace_id=WORKSPACE_ID,
        ).to_json()
    )
    valid["schema_version"] = 2
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope.from_dict(valid)
    assert exc_info.value.code == UNKNOWN_EVENT_VERSION


def test_typed_error_codes_stable_and_mapped() -> None:
    """AC-CONTRACT-07: the registry and the HTTP/SSE projection are real."""
    from app.runtime import EVENT_STALE_SEQ
    from app.runtime.error_mapping import (
        CAPABILITY_DISABLED,
        HTTP_STATUS_BY_ERROR_CODE,
        http_status_for,
        sse_error_frame,
    )

    expected_codes = {
        STATE_TRANSITION_VIOLATION,
        RUN_TERMINAL_STATE,
        UNKNOWN_EVENT_VERSION,
        UNKNOWN_EVENT_TYPE,
        ARTIFACT_PAYLOAD_TOO_LARGE,
        PAYLOAD_NOT_SERIALIZABLE,
        ARTIFACT_REF_INVALID,
        EVENT_ENVELOPE_INVALID,
        IDEMPOTENCY_CONFLICT,
        CAPABILITY_DISABLED,
        EVENT_STALE_SEQ,
    }
    assert set(HTTP_STATUS_BY_ERROR_CODE) == expected_codes
    assert http_status_for(ARTIFACT_PAYLOAD_TOO_LARGE) == 413
    assert http_status_for(UNKNOWN_EVENT_VERSION) == 400
    assert http_status_for("NONEXISTENT_CODE") == 500  # fail-closed

    frame = sse_error_frame(ARTIFACT_PAYLOAD_TOO_LARGE, "payload too large")
    assert frame.splitlines()[0] == "event: error"
    decoded = json.loads(frame.splitlines()[1][len("data: "):])
    assert decoded == {"code": ARTIFACT_PAYLOAD_TOO_LARGE, "message": "payload too large"}
