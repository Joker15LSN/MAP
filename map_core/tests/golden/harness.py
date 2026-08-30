"""Golden-trace harness: scripted FakeLLM + FakeTool + real pipeline runners.

This module drives the REAL map_core pipelines (GlobalDomain / FlowDomain /
MasterPipeline) with fake LLM and fake tool handlers, so the golden fixtures
stay fully offline and deterministic. Typed ``CoreExecutionEvent`` streams are
captured directly with :class:`InMemoryExecutionEventSink` and asserted against
each fixture's ``expected.execution_events`` contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from types import SimpleNamespace
from typing import Any, get_args

from map_core.schema.flow_domain_schema import FlowChatRequest
from map_core.schema.global_domain_schema import GlobalDomainChatSchema
from map_core.schema.master_pipeline_schema import MasterAgentChatSchema
from map_core.service.agent.base import AgentRequest
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agent_dispatcher import AgentDispatcher
from map_core.service.execution_event import (
    CoreExecutionEvent,
    ExecutionEventEmitter,
    ExecutionEventType,
    InMemoryExecutionEventSink,
    RunContext,
    set_run_context,
)
from map_core.service.flow_domain import FlowDomain
from map_core.service.global_domain import GlobalDomain
from map_core.service.master_pipeline import MasterPipeline
from map_core.utils.model_invocation import (
    ModelInvocationEvent,
    ModelInvocationOutcome,
    ModelInvocationRequest,
    ModelInvocationStream,
    ModelUsage,
)

# Frozen typed event type set (runtime view of ExecutionEventType, which is a
# typing.Literal and therefore not directly iterable at runtime).
TYPED_EVENT_TYPES: frozenset[str] = frozenset(get_args(ExecutionEventType))


# ---------------------------------------------------------------------------
# FakeLLM
# ---------------------------------------------------------------------------

_DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5}

_SCRIPT_KINDS = {"asimple_chat", "ask_tool", "asimple_chat_stream", "ainvoke"}


class FakeLLM:
    """Scripted LLM implementing the surface used by every MAP component.

    Script items are matched by ``kind`` first, then by optional selectors:
      - asimple_chat:     item.get("schema_name") must equal the call's
                          ``schema_name`` when the item declares one.
      - ask_tool:         when the item declares ``tool_names``, every name
                          must be present in the call's ``tools`` argument.
    Items without selectors are consumed in declaration order by matching calls.
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.config = SimpleNamespace(
            model="golden-fake-model",
            base_url="http://localhost:9/v1",
            api_key="k",
            context_size=131_072,
            timeout=10.0,
            max_retries=0,
        )
        self.script = list(script)
        self.consumed = [False] * len(self.script)
        self.calls: list[dict[str, Any]] = []

    # -- script matching ----------------------------------------------------
    def _item_has_selector(self, item: dict[str, Any]) -> bool:
        return item.get("schema_name") is not None or "tool_names" in item

    def _matches(self, item: dict[str, Any], selectors: dict[str, Any]) -> bool:
        if "schema_name" in selectors:
            declared = item.get("schema_name")
            if declared is not None and declared != selectors["schema_name"]:
                return False
        if "tool_names" in selectors:
            declared = item.get("tool_names")
            if declared is not None:
                call_names = set(selectors["tool_names"])
                if not set(declared).issubset(call_names):
                    return False
        return True

    def _next(self, kind: str, selectors: dict[str, Any] | None = None) -> dict[str, Any]:
        for idx, item in enumerate(self.script):
            if self.consumed[idx]:
                continue
            if item.get("kind") != kind:
                continue
            if selectors is not None:
                if not self._matches(item, selectors):
                    continue
            else:
                if self._item_has_selector(item):
                    continue
            self.consumed[idx] = True
            return item
        raise AssertionError(
            f"FakeLLM script exhausted: no unconsumed item for kind={kind!r} "
            f"selectors={selectors!r} (calls so far: {self.calls})"
        )

    # -- new typed invoke surface -------------------------------------------
    async def invoke(
        self, req: ModelInvocationRequest
    ) -> ModelInvocationOutcome | ModelInvocationStream:
        if req.stream is True:
            return await self._invoke_stream(req)
        return self._invoke_non_stream(req)

    def _classify(self, req: ModelInvocationRequest) -> tuple[str, dict[str, Any] | None]:
        if req.tools is not None:
            tool_names = []
            for tool in req.tools:
                if isinstance(tool, dict):
                    fn = tool.get("function") or {}
                else:
                    fn = getattr(tool, "function", None)
                if isinstance(fn, dict) and fn.get("name"):
                    tool_names.append(str(fn["name"]))
            return "ask_tool", {"tool_names": tool_names}
        if req.structured is not None:
            return "asimple_chat", {"schema_name": req.structured.name}
        return "ainvoke", None

    def _invoke_non_stream(self, req: ModelInvocationRequest) -> ModelInvocationOutcome:
        kind, selectors = self._classify(req)
        call: dict[str, Any] = {"kind": kind}
        if selectors is not None:
            call.update(selectors)
        self.calls.append(call)
        item = self._next(kind, selectors)
        return self._item_to_outcome(kind, item)

    async def _invoke_stream(
        self, req: ModelInvocationRequest
    ) -> ModelInvocationStream:
        kind = "asimple_chat_stream"
        schema_name = req.structured.name if req.structured is not None else None
        self.calls.append({"kind": kind, "schema_name": schema_name})
        item = self._next(kind, {"schema_name": schema_name})

        async def _events():
            for chunk in item.get("chunks", []):
                yield ModelInvocationEvent(
                    type="content", data={"type": "content", "data": chunk}
                )
            usage = item.get("usage") or dict(_DEFAULT_USAGE)
            yield ModelInvocationEvent(
                type="usage", data={"type": "usage", "data": usage}
            )
            yield ModelInvocationEvent(
                type="terminal",
                status="succeeded",
                data={"attempts": 1, "latency_ms": 0.0},
                usage=self._usage_from_item(item),
            )

        return ModelInvocationStream(_events())

    @staticmethod
    def _usage_from_item(item: dict[str, Any]) -> ModelUsage:
        usage = item.get("usage") or dict(_DEFAULT_USAGE)
        return ModelUsage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )

    def _item_to_outcome(
        self, kind: str, item: dict[str, Any]
    ) -> ModelInvocationOutcome:
        tool_calls = None
        raw_calls = item.get("tool_calls")
        if raw_calls:
            tool_calls = [
                {
                    "id": str(call.get("id") or f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": str(call.get("arguments") or "{}"),
                    },
                }
                for i, call in enumerate(raw_calls)
            ]
        return ModelInvocationOutcome(
            status="succeeded",
            content=item.get("content", ""),
            tool_calls=tool_calls,
            usage=self._usage_from_item(item),
            finish_reason=item.get("finish_reason")
            or ("tool_calls" if tool_calls else "stop"),
            model="golden-fake-model",
            attempts=1,
            latency_ms=0.0,
        )

# ---------------------------------------------------------------------------
# Fake tools
# ---------------------------------------------------------------------------

DEFAULT_TOOL_NAMES = (
    "general_qa_agent",
    "web_search_agent",
    "efficiency_pi_agent",
    "annual_performance_agent",
    "ask_database_agent",
    "wenshu_agent",
    "industry_chat_agent",
    "zhiwen_agent",
)


def _default_tool_handler(args: dict[str, Any], request: AgentRequest, parid: str) -> dict[str, Any]:
    del request, parid
    return {
        "content": "default fake tool result",
        "data_source": {"source": "fake", "args": args},
    }


class FakeToolHarness:
    """Registry of scripted tools plus an execution log for tool-IO assertions."""

    def __init__(self, specs: list[dict[str, Any]]) -> None:
        self.executions: list[dict[str, Any]] = []
        self.registry: dict[str, Tool] = {}
        for spec in specs:
            name = spec["name"]
            self.registry[name] = self._build_tool(name, spec)
        # every well-known tool agent is available with a benign default handler
        for name in DEFAULT_TOOL_NAMES:
            if name not in self.registry:
                self.registry[name] = Tool(
                    name=name,
                    description=f"fake {name}",
                    parameters={"type": "object", "properties": {}},
                    handler=_default_tool_handler,
                )

    def _build_tool(self, name: str, spec: dict[str, Any]) -> Tool:
        outcomes: list[dict[str, Any]] = list(spec.get("returns", []) or [])

        async def handler(
            args: dict[str, Any], request: AgentRequest, parid: str
        ) -> dict[str, Any]:
            self.executions.append(
                {"tool": name, "args": args, "parid": parid, "call_index": len(self.executions)}
            )
            if outcomes:
                outcome = outcomes.pop(0)
            elif spec.get("fail", False):
                outcome = {"content": "", "error": "simulated tool failure"}
            else:
                outcome = {
                    "content": f"{name} output",
                    "data_source": {"source": "fake", "args": args},
                }
            return outcome

        return Tool(
            name=name,
            description=spec.get("description", f"fake tool {name}"),
            parameters=(
                spec.get("parameters")
                or {"type": "object", "properties": {}, "additionalProperties": True}
            ),
            handler=handler,
        )


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------


def _make_typed_recording() -> tuple[RunContext, InMemoryExecutionEventSink]:
    """Create a request-level typed emitter with an in-memory sink."""
    run_context = RunContext(run_id=uuid.uuid4())
    sink = InMemoryExecutionEventSink()
    ExecutionEventEmitter.for_context(run_context, sinks=[sink])
    return run_context, sink


def _execution_event_record(event: CoreExecutionEvent) -> dict[str, Any]:
    """Serialize one typed event for golden assertions.

    ``component`` / ``phase`` / ``status`` are lifted to the top level only
    when the event data actually carries them, so fixtures can assert the
    execution facts without reaching into the full ``data`` payload.
    """
    data = dict(event.data)
    record: dict[str, Any] = {
        "type": event.type,
        "seq": event.seq,
        "data": data,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
    }
    for field in ("component", "phase", "status"):
        if data.get(field) is not None:
            record[field] = data[field]
    return record


def _install_global_components(
    gd: GlobalDomain,
    fake_llm: FakeLLM,
    tools: FakeToolHarness,
) -> None:
    gd.llm = fake_llm
    gd.scene_selector._llm = fake_llm
    gd.summarize_agent.llm = fake_llm
    gd.query_rewriter.llm = fake_llm
    dispatcher = AgentDispatcher(llm=fake_llm, tool_registry=tools.registry)
    gd.agent_dispatcher = dispatcher


def _build_global_request(fixture: dict[str, Any]) -> GlobalDomainChatSchema:
    raw = dict(fixture["request"])
    raw.setdefault("chart_plotting_enabled", False)
    raw.setdefault("content_review_enabled", False)
    raw.setdefault("staff_code", "golden_tester")
    return GlobalDomainChatSchema(**raw)


def run_global(fixture: dict[str, Any]) -> dict[str, Any]:
    """Run the REAL GlobalDomain pipeline_stream with scripted fake components."""
    request = _build_global_request(fixture)

    async def _run() -> dict[str, Any]:
        fake_llm = FakeLLM(fixture.get("llm_script", []))
        tools = FakeToolHarness(fixture.get("tools", []))
        gd = GlobalDomain(llm=fake_llm, request=request, staff_code=request.staff_code)
        _install_global_components(gd, fake_llm, tools)

        run_context, typed_sink = _make_typed_recording()
        events = []
        with set_run_context(run_id=run_context.run_id):
            async for event in gd.pipeline_stream(request):
                events.append(event)
            await ExecutionEventEmitter.current().close()
        return _assemble_result(fixture, events, tools, typed_sink, fake_llm)

    return asyncio.run(_run())


def run_flow(fixture: dict[str, Any]) -> dict[str, Any]:
    request = FlowChatRequest(**dict(fixture["request"]))
    flow_config = fixture["request"].get("flow_config")

    async def _run() -> dict[str, Any]:
        fake_llm = FakeLLM(fixture.get("llm_script", []))
        tools = FakeToolHarness(fixture.get("tools", []))
        fd = FlowDomain(request=request)
        _install_global_components(fd.global_domain, fake_llm, tools)

        run_context, typed_sink = _make_typed_recording()
        if flow_config is not None:
            snapshot = fixture.get("flow_snapshot")
            provider = fd.flow_config_provider
            original = provider.get_snapshot
            provider.get_snapshot = _fake_snapshot_loader(snapshot)
        else:
            original = None
            provider = None
        try:
            events = []
            with set_run_context(run_id=run_context.run_id):
                async for event in fd.pipeline_stream(request):
                    events.append(event)
                await ExecutionEventEmitter.current().close()
        finally:
            if original is not None:
                provider.get_snapshot = original
        return _assemble_result(fixture, events, tools, typed_sink, fake_llm)

    return asyncio.run(_run())


def _fake_snapshot_loader(snapshot: dict[str, Any] | None):
    """Build a static FlowConfigSnapshot with optional scenario/skill packs."""
    from map_core.schema.flow_domain_schema import (
        FlowConfigSchema,
        ScenarioPackSchema,
        SkillDescriptorSchema,
    )
    from map_core.service.flow_config_provider import FlowConfigSnapshot

    def _loader(*args: Any, **kwargs: Any) -> Any:
        scenarios = []
        for item in (snapshot or {}).get("scenario_packs") or []:
            scenarios.append(ScenarioPackSchema(**item))
        skills = []
        for item in (snapshot or {}).get("flow_skill_descriptors") or []:
            skills.append(SkillDescriptorSchema(**item))
        flow_policy_raw = (snapshot or {}).get("flow_policy")
        flow_policy = (
            FlowConfigSchema(**flow_policy_raw) if flow_policy_raw else FlowConfigSchema()
        )
        return FlowConfigSnapshot(
            source="static",
            fetched_at="2025-01-01T00:00:00+08:00",
            updated_at=None,
            flow_policy=flow_policy,
            scenario_packs=scenarios,
            flow_skill_descriptors=skills,
            stale=False,
        )

    async def _async_loader(*args: Any, **kwargs: Any) -> Any:
        return _loader(*args, **kwargs)

    return _async_loader


def run_master(fixture: dict[str, Any]) -> dict[str, Any]:
    request = MasterAgentChatSchema(**dict(fixture["request"]))

    async def _run() -> dict[str, Any]:
        fake_llm = FakeLLM(fixture.get("llm_script", []))
        tools = FakeToolHarness(fixture.get("tools", []))
        mp = MasterPipeline(
            llm=fake_llm,
            request=request,
            staff_code=request.staff_code,
            tool_registry=tools.registry,
        )
        mp.agent_runtime.llm = fake_llm
        mp.agent_runtime.tool_registry = tools.registry

        run_context, typed_sink = _make_typed_recording()
        events = []
        with set_run_context(run_id=run_context.run_id):
            async for event in mp.pipeline_stream(request):
                events.append(event)
            await ExecutionEventEmitter.current().close()
        return _assemble_result(fixture, events, tools, typed_sink, fake_llm)

    return asyncio.run(_run())


def _assemble_result(
    fixture: dict[str, Any],
    events: list[Any],
    tools: FakeToolHarness,
    typed_sink: InMemoryExecutionEventSink,
    fake_llm: FakeLLM,
) -> dict[str, Any]:
    event_records = []
    for event in events:
        data = getattr(event, "data", {})
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        event_records.append({"event": event.event, "data": data})
    return {
        "fixture_id": fixture.get("id"),
        "mode": fixture.get("mode"),
        "engine": fixture.get("engine"),
        "events": event_records,
        "tool_executions": list(tools.executions),
        "execution_events": [
            _execution_event_record(event) for event in typed_sink.events
        ],
        "llm_calls": list(fake_llm.calls),
    }


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX20_RE = re.compile(r"\b[0-9a-f]{20}\b")  # agent_id (secrets.token_hex(10))
_DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_FLOAT_DURATION_RE = re.compile(r"duration_s[\"']?\s*[:=]\s*[0-9]+(?:\.[0-9]+)?")
_GENERIC_FLOAT_RE = re.compile(r"\b\d+\.\d{4,}\b")
_TOKEN_USAGE_RE = re.compile(r"(\"(?:prompt|completion)_tokens\"\s*:\s*)\d+")
_BIG_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b")


def normalize_text(value: Any) -> str:
    """Replace volatile values (ids, timestamps, durations, token counts)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = _UUID_RE.sub("<UUID>", text)
    text = _HEX20_RE.sub("<HEX>", text)
    text = _DATETIME_RE.sub("<TS>", text)
    text = _FLOAT_DURATION_RE.sub("duration_s:<DURATION>", text)
    text = _GENERIC_FLOAT_RE.sub("<FLOAT>", text)
    text = _TOKEN_USAGE_RE.sub(r"\g<1><N>", text)
    text = _BIG_HEX_RE.sub("<HEX>", text)
    return text


def events_summary(result: dict[str, Any]) -> dict[str, Any]:
    """A compact, normalized view of the event stream for assertions."""
    event_types = [item["event"] for item in result["events"]]
    meta_phases = [
        item["data"].get("phase")
        for item in result["events"]
        if item["event"] == "meta" and isinstance(item["data"].get("phase"), str)
    ]
    return {"event_types": event_types, "meta_phases": meta_phases}


def sub_sequence(needle: list[str], haystack: list[str]) -> bool:
    """True when ``needle`` appears in ``haystack`` preserving relative order."""
    it = iter(haystack)
    return all(any(item == candidate for candidate in it) for item in needle)


def find_events(result: dict[str, Any], event: str, **data_filter: Any) -> list[dict[str, Any]]:
    matches = []
    for item in result["events"]:
        if item["event"] != event:
            continue
        if all(item["data"].get(key) == value for key, value in data_filter.items()):
            matches.append(item["data"])
    return matches


def done_data(result: dict[str, Any]) -> dict[str, Any]:
    for item in reversed(result["events"]):
        if item["event"] == "done":
            return item["data"]
    raise AssertionError("no done event in stream")


# ---------------------------------------------------------------------------
# Runtime config hash: freezes the fixture's execution contract.
# ---------------------------------------------------------------------------


def compute_runtime_hash(fixture: dict[str, Any]) -> str:
    """sha256 over the fixture fields that determine what the pipeline executes."""
    contract = {
        "mode": fixture.get("mode"),
        "engine": fixture.get("engine"),
        "request": _stable_request(fixture.get("request", {})),
        "llm_script": fixture.get("llm_script", []),
        "tools": fixture.get("tools", []),
        "flow_snapshot": fixture.get("flow_snapshot"),
    }
    payload = json.dumps(contract, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _stable_request(request: dict[str, Any]) -> dict[str, Any]:
    stable = dict(request)
    stable.pop("staff_code", None)
    stable.pop("backend_env", None)
    return stable


def verify_runtime_hash(fixture: dict[str, Any]) -> None:
    expected = fixture.get("runtime_config_hash")
    if not expected:
        raise AssertionError("fixture missing runtime_config_hash")
    actual = compute_runtime_hash(fixture)
    if actual != expected:
        raise AssertionError(
            f"fixture runtime_config_hash mismatch: expected {expected!r} got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Golden assertions (pure functions so they can be reused on mutated results)
# ---------------------------------------------------------------------------


def _agent_action_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in result["events"]:
        if item["event"] != "meta":
            continue
        if item["data"].get("phase") != "agent_action":
            continue
        for agent in item["data"].get("agents") or []:
            records.append(agent)
    return records


def _agent_result_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in result["events"]:
        if item["event"] != "meta":
            continue
        if item["data"].get("phase") != "agent_result":
            continue
        for agent in item["data"].get("agents") or []:
            records.append(agent)
    return records


def assert_golden_result(result: dict[str, Any], fixture: dict[str, Any]) -> None:
    """Assert the run result matches the fixture's expected contract."""
    expected = fixture.get("expected", {})
    _assert_expected_event_types(result, expected, fixture)
    _assert_expected_meta_phases(result, expected, fixture)
    _assert_scene_selected_agents_contract(result, expected, fixture)
    _assert_low_confidence_scene(result, expected, fixture)
    _assert_agent_result_agents(result, expected, fixture)
    _assert_tool_io_contract(result, expected, fixture)
    _assert_failed_tool_results(result, expected, fixture)
    _assert_final_content(result, expected, fixture)
    _assert_execution_events(result, expected.get("execution_events"), fixture)
    _assert_flow_contract(result, expected.get("flow"), fixture)
    if expected.get("no_content_delta"):
        deltas = [item for item in result["events"] if item["event"] == "content_delta"]
        assert not deltas, f"[{fixture['id']}] content_delta should be absent"


def _assert_expected_event_types(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    expected_types = expected.get("event_types") or []
    actual_types = [item["event"] for item in result["events"]]
    if expected_types:
        assert sub_sequence(expected_types, actual_types), (
            f"[{fixture['id']}] event order changed or event missing: "
            f"expected sub-sequence {expected_types} not found in {actual_types}"
        )


def _assert_expected_meta_phases(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    expected_phases = expected.get("meta_phases") or []
    actual_phases = [
        item["data"].get("phase")
        for item in result["events"]
        if item["event"] == "meta" and isinstance(item["data"].get("phase"), str)
    ]
    if expected_phases:
        assert sub_sequence(expected_phases, actual_phases), (
            f"[{fixture['id']}] meta phase order changed or phase missing: "
            f"expected {expected_phases} not in {actual_phases}"
        )


def _assert_scene_selected_agents_contract(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    scene_agents = _scene_selected_agents(result)
    expected_scene_agents = expected.get("scene_selected_agents") or []
    for code in expected_scene_agents:
        assert code in scene_agents, (
            f"[{fixture['id']}] scene_selected missing agent {code!r} in {scene_agents}"
        )


def _assert_low_confidence_scene(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    low_conf = expected.get("scene_selected_low_confidence")
    if not low_conf:
        return
    scene_events = [
        item["data"]
        for item in result["events"]
        if item["event"] == "meta" and item["data"].get("phase") == "scene_selected"
    ]
    assert scene_events, f"[{fixture['id']}] missing scene_selected event"
    matched = False
    for scene in scene_events:
        scene_result = scene.get("scene_result") or {}
        for sub in scene_result.get("sub_scenes") or []:
            if low_conf["agent_code"] in (sub.get("sub_scenes") or []):
                conf = sub.get("confidence")
                reason = str(sub.get("reason") or "")
                reason_contains = low_conf.get("reason_contains")
                if reason_contains is None:
                    reason_ok = True
                elif isinstance(reason_contains, str):
                    reason_ok = reason_contains in reason
                else:
                    reason_ok = all(str(needle) in reason for needle in reason_contains)
                matched = conf == low_conf["confidence"] and reason_ok
                break
        if matched:
            break
    assert matched, (
        f"[{fixture['id']}] low-confidence scene contract not matched: "
        f"{low_conf!r}"
    )


def _assert_agent_result_agents(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    result_agents = [rec.get("agent_code") for rec in _agent_result_records(result)]
    expected_result_agents = expected.get("agent_result_agents") or []
    if not expected_result_agents:
        return
    if expected.get("agents_order_independent"):
        for code in expected_result_agents:
            assert code in result_agents, (
                f"[{fixture['id']}] agent_result missing agent {code!r} in {result_agents}"
            )
    else:
        assert result_agents == expected_result_agents, (
            f"[{fixture['id']}] agent_result agents mismatch: "
            f"expected {expected_result_agents!r} got {result_agents!r}"
        )


def _assert_tool_io_contract(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    action_records = _agent_action_records(result)
    for spec in expected.get("tool_io") or []:
        count = sum(
            1
            for rec in action_records
            if rec.get("action") == spec["action"]
            and rec.get("tool_name") == spec["tool"]
        )
        assert count == spec["count"], (
            f"[{fixture['id']}] tool IO mismatch: {spec} expected count "
            f"{spec['count']} got {count}"
        )


def _assert_failed_tool_results(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    failed = expected.get("tool_result_failed")
    if not failed:
        return
    action_records = _agent_action_records(result)
    count = sum(
        1
        for rec in action_records
        if rec.get("action") == "tool_result"
        and rec.get("status") == "failed"
        and rec.get("tool_name") == failed["tool"]
    )
    assert count == failed["count"], (
        f"[{fixture['id']}] failed tool_result count mismatch for "
        f"{failed['tool']}: expected {failed['count']} got {count}"
    )


def _assert_final_content(
    result: dict[str, Any], expected: dict[str, Any], fixture: dict[str, Any]
) -> None:
    final = expected.get("final_content") or {}
    content = done_data(result).get("content") or ""
    if final.get("exact") is not None:
        assert content == final["exact"], (
            f"[{fixture['id']}] final content exact mismatch: "
            f"expected {final['exact']!r} got {content!r}"
        )
    for needle in final.get("contains") or []:
        assert needle in content, (
            f"[{fixture['id']}] final content missing {needle!r}: {content!r}"
        )


def _scene_selected_agents(result: dict[str, Any]) -> list[str]:
    agents: list[str] = []
    for item in result["events"]:
        if item["event"] == "meta" and item["data"].get("phase") == "scene_selected":
            for agent in item["data"].get("agents") or []:
                code = agent.get("agent_code")
                if code and code not in agents:
                    agents.append(code)
    return agents


def _assert_execution_events(
    result: dict[str, Any], spec: list[dict[str, Any]] | None, fixture: dict[str, Any]
) -> None:
    """Assert the typed execution-event stream against the fixture contract.

    The fixture list freezes the full typed type sequence (event count and
    order).  In addition, seq must be monotonic starting at 1 and every
    record must carry trace/span fields (both None or both populated).
    Finally each fixture item's key ``data`` fields and optional
    ``component`` / ``phase`` / ``status`` are matched as a subset of the
    recorded event.
    """
    if not spec:
        return
    actual = result.get("execution_events") or []
    expected_types = [item["type"] for item in spec]
    actual_types = [item["type"] for item in actual]
    assert actual_types == expected_types, (
        f"[{fixture['id']}] typed execution event sequence mismatch: "
        f"expected {expected_types!r} got {actual_types!r}"
    )

    seqs = [item["seq"] for item in actual]
    assert seqs == list(range(1, len(actual) + 1)), (
        f"[{fixture['id']}] execution event seq not monotonic from 1: {seqs!r}"
    )

    for item in actual:
        assert "trace_id" in item and "span_id" in item, (
            f"[{fixture['id']}] execution event missing trace_id/span_id fields"
        )
        assert (item["trace_id"] is None) == (item["span_id"] is None), (
            f"[{fixture['id']}] execution event trace/span presence mismatch: "
            f"trace_id={item['trace_id']!r} span_id={item['span_id']!r}"
        )

    for expected_item, actual_item in zip(spec, actual):
        assert actual_item["type"] == expected_item["type"], (
            f"[{fixture['id']}] typed event type mismatch at seq "
            f"{actual_item['seq']}: expected {expected_item['type']!r} "
            f"got {actual_item['type']!r}"
        )
        for field in ("component", "phase", "status"):
            if field in expected_item:
                assert actual_item.get(field) == expected_item[field], (
                    f"[{fixture['id']}] typed event {actual_item['type']} "
                    f"seq={actual_item['seq']} {field} mismatch: "
                    f"expected {expected_item[field]!r} got {actual_item.get(field)!r}"
                )
        expected_data = expected_item.get("data") or {}
        actual_data = actual_item.get("data") or {}
        for key, expected_value in expected_data.items():
            assert actual_data.get(key) == expected_value, (
                f"[{fixture['id']}] typed event {actual_item['type']} "
                f"seq={actual_item['seq']} data.{key} mismatch: "
                f"expected {expected_value!r} got {actual_data.get(key)!r}"
            )


def _assert_flow_contract(
    result: dict[str, Any], spec: dict[str, Any] | None, fixture: dict[str, Any]
) -> None:
    if not spec:
        return
    events = result["events"]

    def _meta_events(phase: str) -> list[dict[str, Any]]:
        return [
            item["data"]
            for item in events
            if item["event"] == "meta" and item["data"].get("phase") == phase
        ]

    done = done_data(result)
    flow_meta = (done.get("meta") or {}).get("flow") or {}
    _assert_flow_scenarios(flow_meta, spec, fixture)
    _assert_flow_node_verdicts(_meta_events, spec, fixture)
    _assert_flow_repair_count(flow_meta, spec, fixture)
    _assert_flow_graph_incomplete(_meta_events, spec, fixture)
    _assert_flow_repair_applied(_meta_events, spec, fixture)
    _assert_flow_skill_authorization(_meta_events, spec, fixture)
    _assert_flow_capabilities(_meta_events, spec, fixture)


def _assert_flow_scenarios(
    flow_meta: dict[str, Any], spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    expected_scenarios = spec.get("scenarios") or []
    actual_scenarios = flow_meta.get("scenarios") or []
    for scenario_id in expected_scenarios:
        assert scenario_id in actual_scenarios, (
            f"[{fixture['id']}] flow scenarios missing {scenario_id!r} in {actual_scenarios}"
        )


def _assert_flow_node_verdicts(
    _meta_events: Any, spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    for verdict_spec in spec.get("node_verdicts") or []:
        needle = verdict_spec["node_contains"]
        verdict = verdict_spec["verdict"]
        matches = []
        for data in _meta_events("flow_node_result"):
            node_id = (data.get("node_result") or {}).get("node_id") or ""
            step_verdict = (data.get("step_verdict") or {}).get("verdict") or ""
            if needle in node_id:
                matches.append((node_id, step_verdict))
        assert matches, (
            f"[{fixture['id']}] flow node result missing node containing {needle!r}"
        )
        assert any(v == verdict for _n, v in matches), (
            f"[{fixture['id']}] flow verdict for {needle!r} expected {verdict!r}, got {matches}"
        )


def _assert_flow_repair_count(
    flow_meta: dict[str, Any], spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    if "repair_count" in spec:
        actual_repairs = flow_meta.get("repair_count")
        assert actual_repairs == spec["repair_count"], (
            f"[{fixture['id']}] repair_count expected {spec['repair_count']} got {actual_repairs}"
        )


def _assert_flow_graph_incomplete(
    _meta_events: Any, spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    incomplete = spec.get("graph_incomplete")
    incomplete_events = _meta_events("flow_graph_incomplete")
    if incomplete is None:
        assert not incomplete_events, (
            f"[{fixture['id']}] flow_graph_incomplete should be absent"
        )
        return
    assert incomplete_events, f"[{fixture['id']}] missing flow_graph_incomplete event"
    for data in incomplete_events:
        remaining = " ".join(str(x) for x in data.get("remaining_nodes") or [])
        for needle in incomplete.get("remaining_contains") or []:
            assert needle in remaining, (
                f"[{fixture['id']}] graph_incomplete remaining missing {needle!r} in {remaining}"
            )
        if incomplete.get("reason"):
            assert data.get("reason") == incomplete["reason"], (
                f"[{fixture['id']}] graph_incomplete reason mismatch"
            )


def _assert_flow_repair_applied(
    _meta_events: Any, spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    if "repair_applied" in spec:
        present = bool(_meta_events("flow_repair_applied"))
        assert present is spec["repair_applied"], (
            f"[{fixture['id']}] flow_repair_applied presence mismatch"
        )


def _assert_flow_skill_authorization(
    _meta_events: Any, spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    for auth_spec in spec.get("skill_authorization") or []:
        needle = auth_spec["node_contains"]
        matched = False
        for data in _meta_events("skill_authorization"):
            node_id = str(data.get("node_id") or "")
            if needle not in node_id:
                continue
            authorized_tools = [
                str(item.get("tool_name") or "")
                for item in data.get("authorized_skills") or []
            ]
            if all(
                tool in authorized_tools
                for tool in auth_spec.get("authorized_tools_contains") or []
            ):
                matched = True
                break
        assert matched, (
            f"[{fixture['id']}] skill_authorization contract not matched for {auth_spec!r}"
        )


def _assert_flow_capabilities(
    _meta_events: Any, spec: dict[str, Any], fixture: dict[str, Any]
) -> None:
    for data in _meta_events("flow_node_started"):
        node = data.get("node") or {}
        capabilities = node.get("allowed_capabilities") or []
        node_id = str(node.get("node_id") or "")
        for tool in spec.get("capabilities_contains") or []:
            assert tool in capabilities, (
                f"[{fixture['id']}] node {node_id} capabilities missing {tool!r} in {capabilities}"
            )


def assert_engine_parity(legacy: dict[str, Any], scope: dict[str, Any]) -> None:
    """Both engines must expose the same observable contract."""
    assert legacy["events"] and scope["events"], "both engine runs must emit events"
    legacy_types = [item["event"] for item in legacy["events"]]
    scope_types = [item["event"] for item in scope["events"]]
    assert legacy_types == scope_types, (
        f"engine parity event_types mismatch: legacy={legacy_types} scope={scope_types}"
    )
    legacy_phases = [
        item["data"].get("phase")
        for item in legacy["events"]
        if item["event"] == "meta" and isinstance(item["data"].get("phase"), str)
    ]
    scope_phases = [
        item["data"].get("phase")
        for item in scope["events"]
        if item["event"] == "meta" and isinstance(item["data"].get("phase"), str)
    ]
    assert legacy_phases == scope_phases, (
        f"engine parity meta phases mismatch: legacy={legacy_phases} scope={scope_phases}"
    )
    legacy_agents = sorted(
        {rec.get("agent_code") for rec in _agent_result_records(legacy)}
    )
    scope_agents = sorted({rec.get("agent_code") for rec in _agent_result_records(scope)})
    assert legacy_agents == scope_agents, (
        f"engine parity agent_result agents mismatch: {legacy_agents} vs {scope_agents}"
    )
    assert done_data(legacy).get("content") == done_data(scope).get("content"), (
        "engine parity final content mismatch"
    )
