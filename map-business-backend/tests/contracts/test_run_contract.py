"""P0-CONTRACT-01 contract tests (AC-CONTRACT-01/03/05/07/08).

- AC-CONTRACT-01: exhaustive transition-table coverage for all five machines
  (legal transitions pass, illegal / terminal out-edges / unknown states fail).
- AC-CONTRACT-03: SSE frame shape + client dedupe key semantics.
- AC-CONTRACT-05: 64KiB boundary (<=65536 inline, >65536 ArtifactRef-only).
- AC-CONTRACT-07: typed error codes stable.
- AC-CONTRACT-08: unknown major/event-type fail closed; minor forward compat.
"""

from __future__ import annotations

import json

import pytest

from app.runtime import (
    ARTIFACT_PAYLOAD_TOO_LARGE,
    EVENT_SCHEMA_VERSION,
    IDEMPOTENCY_CONFLICT,
    RUN_TERMINAL_STATE,
    STATE_TRANSITION_VIOLATION,
    UNKNOWN_EVENT_TYPE,
    UNKNOWN_EVENT_VERSION,
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
            with pytest.raises(Exception) as exc_info:
                validate_transition(machine, current, target)
            assert str(exc_info.value).startswith(STATE_TRANSITION_VIOLATION), (
                machine,
                current,
                target,
            )
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


def test_run_cancel_race_converges_to_allowed_terminal() -> None:
    """cancel/done/timeout race: loser must fail closed, never invent states."""
    assert run_cancel_allowed_from("running")
    assert run_cancel_allowed_from("queued")
    assert run_cancel_allowed_from("paused")
    assert not run_cancel_allowed_from("completed")
    assert not run_cancel_allowed_from("cancelled")
    # done wins first -> cancel attempt on terminal fails closed
    with pytest.raises(StateTransitionError) as exc_info:
        validate_transition("run", "completed", "cancelling")
    assert str(exc_info.value).startswith(STATE_TRANSITION_VIOLATION)
    # cancel wins first -> cancelling converges to cancelled only
    validate_transition("run", "running", "cancelling")
    validate_transition("run", "cancelling", "cancelled")
    with pytest.raises(StateTransitionError):
        validate_transition("run", "cancelling", "completed")


def test_event_envelope_sse_frame_and_dedupe_key() -> None:
    env = EventEnvelope.build(
        run_id="11111111-1111-1111-1111-111111111111",
        seq=7,
        event_type="run.started",
        workspace_id="22222222-2222-2222-2222-222222222222",
        data={"hello": "world"},
    )
    frame = env.sse_frame()
    lines = frame.splitlines()
    assert lines[0] == "id: 7"
    assert lines[1] == "event: run.started"
    payload = json.loads(lines[2][len("data: "):])
    # client dedupe key = (run_id, seq)
    assert (payload["run_id"], payload["seq"]) == (
        "11111111-1111-1111-1111-111111111111",
        7,
    )
    # replayed frame keeps the same key so clients can dedupe terminal events
    replay = EventEnvelope.build(
        run_id="11111111-1111-1111-1111-111111111111",
        seq=7,
        event_type="run.started",
        workspace_id="22222222-2222-2222-2222-222222222222",
    )
    assert (replay.run_id, replay.seq) == (env.run_id, env.seq)


def test_event_envelope_seq_must_be_positive() -> None:
    with pytest.raises(ValueError):
        EventEnvelope.build(
            run_id="11111111-1111-1111-1111-111111111111",
            seq=0,
            event_type="run.started",
            workspace_id="22222222-2222-2222-2222-222222222222",
        )


def test_payload_64k_boundary() -> None:
    """json.dumps({"x": s}) is exactly 9 + len(s) bytes; boundary must be exact."""
    # at the limit (9 + 65527 = 65536) -> allowed inline
    assert validate_payload_size({"x": "a" * 65527}) == 65536
    # one byte over (9 + 65528 = 65537) -> ARTIFACT_PAYLOAD_TOO_LARGE
    with pytest.raises(EventEnvelopeError) as exc_info:
        validate_payload_size({"x": "a" * 65528})
    assert exc_info.value.code == ARTIFACT_PAYLOAD_TOO_LARGE
    # smaller payloads stay inline
    assert validate_payload_size({"x": "a" * 65526}) == 65535


def test_payload_over_limit_requires_artifact_ref() -> None:
    """Oversized payload must travel via ArtifactRef manifest, not inline."""
    from app.runtime import ArtifactRef

    ref = ArtifactRef(
        artifact_id="33333333-3333-3333-3333-333333333333",
        workspace_id="22222222-2222-2222-2222-222222222222",
        sha256="a" * 64,
        size_bytes=123456,
        content_type="application/octet-stream",
        policy_labels=("internal",),
    )
    manifest = ref.to_dict()
    for required in (
        "artifact_id",
        "workspace_id",
        "sha256",
        "size_bytes",
        "content_type",
        "created_at",
        "expires_at",
    ):
        assert required in manifest
    assert manifest["policy_labels"] == ["internal"]


def test_unknown_event_version_fails_closed() -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope(
            schema_version=99,
            schema_minor=0,
            event_id="e",
            run_id="r",
            seq=1,
            type="run.started",
            occurred_at="now",
            workspace_id="w",
        )
    assert exc_info.value.code == UNKNOWN_EVENT_VERSION


def test_unknown_event_type_fails_closed() -> None:
    with pytest.raises(EventEnvelopeError) as exc_info:
        EventEnvelope.build(
            run_id="r",
            seq=1,
            event_type="run.not_a_real_event",
            workspace_id="w",
        )
    assert exc_info.value.code == UNKNOWN_EVENT_TYPE
    # unknown prefix also fails closed
    with pytest.raises(EventEnvelopeError) as exc_info2:
        EventEnvelope.build(
            run_id="r", seq=1, event_type="mystery.explosion", workspace_id="w"
        )
    assert exc_info2.value.code == UNKNOWN_EVENT_TYPE


def test_minor_version_forward_compatible() -> None:
    """Unknown minor fields are preserved, never dropped."""
    env = EventEnvelope.build(
        run_id="r",
        seq=1,
        event_type="run.started",
        workspace_id="w",
        schema_minor=7,
        data={"future_field": "kept"},
    )
    payload = env.to_dict()
    assert payload["schema_version"] == EVENT_SCHEMA_VERSION
    assert payload["schema_minor"] == 7
    assert payload["data"]["future_field"] == "kept"


def test_typed_error_codes_stable() -> None:
    """AC-CONTRACT-07: the code registry must not drift silently."""
    expected = {
        STATE_TRANSITION_VIOLATION,
        RUN_TERMINAL_STATE,
        UNKNOWN_EVENT_VERSION,
        UNKNOWN_EVENT_TYPE,
        ARTIFACT_PAYLOAD_TOO_LARGE,
        IDEMPOTENCY_CONFLICT,
    }
    from app.runtime import EVENT_STALE_SEQ

    expected.add(EVENT_STALE_SEQ)
    assert expected == {
        "STATE_TRANSITION_VIOLATION",
        "RUN_TERMINAL_STATE",
        "UNKNOWN_EVENT_VERSION",
        "UNKNOWN_EVENT_TYPE",
        "ARTIFACT_PAYLOAD_TOO_LARGE",
        "IDEMPOTENCY_CONFLICT",
        "EVENT_STALE_SEQ",
    }
