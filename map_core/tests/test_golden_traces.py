"""F-AS-01 golden trace & cross-engine contract baseline.

Drives the REAL GlobalDomain / FlowDomain pipelines with scripted fake LLM and
tool handlers (offline, no external services) and verifies each golden fixture's
SSE event order / key fields / tool IO / typed execution-event stream / final
content semantics. A subset of fixtures is executed on both engines
(legacy + agentscope) to pin the cross-engine contract.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from golden import harness  # noqa: E402

FIXTURES_DIR = TESTS_DIR / "golden" / "fixtures"

# Global / flow fixtures that are actually executed (remaining fixtures are also
# executed; ENGINE_PARITY_ONLY lists are kept for documentation purposes).
EXECUTED_GLOBAL = [
    "global_plain",
    "global_single_tool",
    "global_multi_tool",
    "global_multi_agent",
    "global_tool_failure",
    "global_low_confidence",
]
EXECUTED_FLOW = [
    "flow_serial",
    "flow_condition_fail",
    "flow_repair",
    "flow_fallback",
    "flow_hard_fail",
    "flow_dynamic_skill",
]

# Fixtures that must also run on the OTHER engine and match the first engine.
# (engine_parity in each fixture also marks this.)
ENGINE_PARITY_GLOBAL = ["global_plain", "global_single_tool"]
ENGINE_PARITY_FLOW = ["flow_serial", "flow_repair", "flow_fallback"]

# Fixtures provided as golden baselines but intentionally not executed offline
# (documented reason; kept so the golden corpus is complete).
SKIPPED_WITH_REASON = {
    # none currently: all 12 fixtures are executed below.
}


def _load_fixture(fixture_id: str) -> dict:
    path = FIXTURES_DIR / f"{fixture_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def fixtures() -> dict[str, dict]:
    loaded = {}
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        loaded[data["id"]] = data
    return loaded


@pytest.fixture()
def fixture_ids(fixtures: dict[str, dict]) -> list[str]:
    return sorted(fixtures)


# ---------------------------------------------------------------------------
# Runtime-config hash: the fixture freezes the executed contract.
# ---------------------------------------------------------------------------


def test_runtime_config_hash_self_consistent(fixture_ids: list[str]) -> None:
    for fixture_id in fixture_ids:
        fixture = _load_fixture(fixture_id)
        harness.verify_runtime_hash(fixture)


# ---------------------------------------------------------------------------
# Global domain golden traces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", EXECUTED_GLOBAL)
def test_global_golden_trace(fixture_id: str) -> None:
    fixture = _load_fixture(fixture_id)
    assert fixture["mode"] == "global"
    result = harness.run_global(fixture)
    harness.assert_golden_result(result, fixture)


@pytest.mark.parametrize("fixture_id", EXECUTED_FLOW)
def test_flow_golden_trace(fixture_id: str) -> None:
    fixture = _load_fixture(fixture_id)
    assert fixture["mode"] == "flow"
    result = harness.run_flow(fixture)
    harness.assert_golden_result(result, fixture)


# ---------------------------------------------------------------------------
# Cross-engine contract parity
# ---------------------------------------------------------------------------


def _switch_engine(fixture: dict, engine: str) -> dict:
    clone = copy.deepcopy(fixture)
    clone["engine"] = engine
    clone["request"].setdefault("dispatch_config", {})["engine"] = engine
    return clone


@pytest.mark.parametrize("fixture_id", ENGINE_PARITY_GLOBAL)
def test_global_cross_engine_parity(fixture_id: str) -> None:
    fixture = _load_fixture(fixture_id)
    assert fixture["engine"] == "legacy", "parity baseline must be legacy"
    legacy = harness.run_global(fixture)
    scope = harness.run_global(_switch_engine(fixture, "agentscope"))
    harness.assert_golden_result(legacy, fixture)
    harness.assert_golden_result(scope, _switch_engine(fixture, "agentscope"))
    harness.assert_engine_parity(legacy, scope)


@pytest.mark.parametrize("fixture_id", ENGINE_PARITY_FLOW)
def test_flow_cross_engine_parity(fixture_id: str) -> None:
    fixture = _load_fixture(fixture_id)
    assert fixture["engine"] == "legacy", "parity baseline must be legacy"
    legacy = harness.run_flow(fixture)
    scope = harness.run_flow(_switch_engine(fixture, "agentscope"))
    harness.assert_golden_result(legacy, fixture)
    harness.assert_golden_result(scope, _switch_engine(fixture, "agentscope"))
    harness.assert_engine_parity(legacy, scope)


# ---------------------------------------------------------------------------
# Failure rules: the assertions must FAIL on contract violations.
# ---------------------------------------------------------------------------


def test_failure_rule_missing_event_detected() -> None:
    fixture = _load_fixture("flow_serial")
    result = harness.run_flow(fixture)
    # drop every flow_node_result event -> meta-phase contract broken
    mutated = copy.deepcopy(result)
    mutated["events"] = [
        item
        for item in result["events"]
        if not (
            item["event"] == "meta" and item["data"].get("phase") == "flow_node_result"
        )
    ]
    with pytest.raises(AssertionError, match="phase missing"):
        harness.assert_golden_result(mutated, fixture)


def test_failure_rule_event_order_changed_detected() -> None:
    fixture = _load_fixture("global_plain")
    result = harness.run_global(fixture)
    mutated = copy.deepcopy(result)
    # move every content_delta to the front -> event order broken
    deltas = [item for item in mutated["events"] if item["event"] == "content_delta"]
    assert deltas, "fixture must emit content_delta events"
    mutated["events"] = [
        item for item in mutated["events"] if item["event"] != "content_delta"
    ]
    mutated["events"].insert(0, deltas[0])
    with pytest.raises(AssertionError, match="event order changed or event missing"):
        harness.assert_golden_result(mutated, fixture)


def test_failure_rule_permission_changed_detected() -> None:
    fixture = _load_fixture("flow_dynamic_skill")
    result = harness.run_flow(fixture)
    mutated = copy.deepcopy(result)
    for item in mutated["events"]:
        if item["event"] != "meta":
            continue
        if item["data"].get("phase") != "skill_authorization":
            continue
        item["data"]["authorized_skills"] = [
            skill
            for skill in item["data"].get("authorized_skills") or []
            if str(skill.get("tool_name") or "") != "skill__market_sentiment"
        ]
    with pytest.raises(AssertionError, match="skill_authorization contract"):
        harness.assert_golden_result(mutated, fixture)


def test_failure_rule_evidence_lost_detected() -> None:
    fixture = _load_fixture("flow_serial")
    result = harness.run_flow(fixture)
    mutated = copy.deepcopy(result)
    for item in mutated["events"]:
        if item["event"] != "meta":
            continue
        if item["data"].get("phase") != "flow_node_result":
            continue
        verdict = item["data"].get("step_verdict") or {}
        if "Supply_Chain" in str((item["data"].get("node_result") or {}).get("node_id")):
            verdict["verdict"] = "uncertain"
    with pytest.raises(AssertionError, match="flow verdict"):
        harness.assert_golden_result(mutated, fixture)


def test_failure_rule_tool_io_lost_detected() -> None:
    fixture = _load_fixture("global_single_tool")
    result = harness.run_global(fixture)
    mutated = copy.deepcopy(result)
    # drop web_search_agent tool_call action records
    for item in mutated["events"]:
        if item["event"] != "meta" or item["data"].get("phase") != "agent_action":
            continue
        item["data"]["agents"] = [
            rec
            for rec in item["data"].get("agents") or []
            if not (rec.get("action") == "tool_call" and rec.get("tool_name") == "web_search_agent")
        ]
    with pytest.raises(AssertionError, match="tool IO mismatch"):
        harness.assert_golden_result(mutated, fixture)


# ---------------------------------------------------------------------------
# Corpus inventory: every provided fixture is accounted for.
# ---------------------------------------------------------------------------


def test_golden_corpus_inventory(fixture_ids: list[str]) -> None:
    executed = set(EXECUTED_GLOBAL) | set(EXECUTED_FLOW)
    skipped = set(SKIPPED_WITH_REASON)
    assert executed.isdisjoint(skipped), "fixture cannot be both executed and skipped"
    unknown_executed = executed - set(fixture_ids)
    assert not unknown_executed, f"unknown executed fixture ids: {unknown_executed}"
    unaccounted = set(fixture_ids) - executed - skipped
    assert not unaccounted, f"fixtures neither executed nor documented as skipped: {unaccounted}"
    assert len(executed) >= 12, "must provide at least 12 golden fixtures"


# ---------------------------------------------------------------------------
# Normalizer: volatile values (random ids / timestamps / durations / token
# counts) must be deterministically replaced so traces stay comparable.
# ---------------------------------------------------------------------------


def test_normalizer_replaces_volatile_values() -> None:
    raw = (
        "state_id=24c59c73-5bdc-4eb7-9d1f-49f1e118474b "
        "graph_id=beg_c646f5ecff7d47cebe12871765b8a84f "
        "agent_id=abcdef0123456789abcdef0123456789abcdef01 "
        "ts=2026-08-09T01:02:03 123.456789 duration_s:0.123456 "
        '{"prompt_tokens": 30, "completion_tokens": 8}'
    )
    out = harness.normalize_text(raw)
    assert "<UUID>" in out, f"uuid not normalized: {out}"
    assert "<HEX>" in out, f"hex id not normalized: {out}"
    assert "<TS>" in out, f"timestamp not normalized: {out}"
    assert "duration_s:<DURATION>" in out, f"duration not normalized: {out}"
    assert "<FLOAT>" in out, f"float not normalized: {out}"
    assert '"prompt_tokens": <N>' in out, f"token count not normalized: {out}"
    assert '"completion_tokens": <N>' in out, f"token count not normalized: {out}"
    # stable content must survive normalization untouched
    stable = harness.normalize_text("公司年度经营目标为稳中求进、利润增长15%。")
    assert "公司年度经营目标" in stable


# ---------------------------------------------------------------------------
# Fixture structure contract: every fixture freezes the same envelope fields.
# ---------------------------------------------------------------------------


def test_fixture_structure_contract(fixtures: dict[str, dict]) -> None:
    assert len(fixtures) >= 12
    for fixture_id, data in fixtures.items():
        assert data["id"] == fixture_id
        assert data["mode"] in {"global", "flow"}
        assert data["engine"] in {"legacy", "agentscope"}
        assert isinstance(data.get("request"), dict), f"{fixture_id}: missing request"
        assert isinstance(data.get("runtime_config_hash"), str), (
            f"{fixture_id}: missing frozen runtime_config_hash"
        )
        assert isinstance(data.get("llm_script"), list), (
            f"{fixture_id}: missing llm_script"
        )
        assert isinstance(data.get("tools"), list), f"{fixture_id}: missing tools"
        expected = data.get("expected") or {}
        assert isinstance(expected.get("event_types"), list) and expected["event_types"], (
            f"{fixture_id}: expected.event_types must be non-empty"
        )
        assert isinstance(expected.get("final_content"), dict), (
            f"{fixture_id}: expected.final_content must be present"
        )
        assert "contains" in expected.get("final_content", {}), (
            f"{fixture_id}: final_content must carry semantic assertions"
        )
        assert "mongo_events" not in expected, (
            f"{fixture_id}: expected.mongo_events must be retyped to "
            "expected.execution_events"
        )
        execution_events = expected.get("execution_events")
        assert isinstance(execution_events, list) and execution_events, (
            f"{fixture_id}: expected.execution_events must be a non-empty list"
        )
        typed_types = [item.get("type") for item in execution_events]
        assert all(isinstance(item.get("type"), str) for item in execution_events), (
            f"{fixture_id}: every execution_events item must carry a str type"
        )
        assert all(item_type in harness.TYPED_EVENT_TYPES for item_type in typed_types), (
            f"{fixture_id}: execution_events contains unknown typed event type "
            f"in {typed_types}"
        )
        assert all(isinstance(item.get("data"), dict) for item in execution_events), (
            f"{fixture_id}: every execution_events item must carry a data dict"
        )
        # no secrets / tokens / real credentials inside fixtures
        blob = json.dumps(data, ensure_ascii=False)
        assert "sk-" not in blob and "Bearer " not in blob, (
            f"{fixture_id}: fixture must not contain secrets"
        )
