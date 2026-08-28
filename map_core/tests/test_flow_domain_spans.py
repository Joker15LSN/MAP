"""P3 acceptance tests: flow-mode engine switch + execution graph spans.

Verifies that each graph node gets a CHAIN span whose parent reflects the
execution-graph topology, verdict/repair are recorded as span events, the SSE
phase contract is unchanged, and dispatch_config.engine is forwarded to the
agent runtime for flow nodes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from map_core.observability.telemetry import current_trace_context
from map_core.schema.flow_domain_schema import (
    BusinessExecutionGraphSchema,
    FlowChatRequest,
    FlowConfigSchema,
    FlowDomainStreamEvent,
    GraphNodeSchema,
    ScenarioPackSchema,
)
from map_core.schema.global_domain_schema import AgentDispatchConfigSchema
from map_core.service import flow_domain as flow_domain_module
from map_core.service.agent.base import AgentResult
from map_core.service.flow_domain import FlowDomain
from map_core.service.skill_hub import SkillMountPlan


class _DummyStateStore:
    async def record_event(self, **_: Any) -> None:
        return None


async def _collect_events(stream: AsyncGenerator[Any, None]) -> list[Any]:
    items: list[Any] = []
    async for item in stream:
        items.append(item)
    return items


def _install_test_tracer(monkeypatch) -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        flow_domain_module,
        "_flow_tracer",
        provider.get_tracer("flow-test"),
    )
    return provider, exporter


def _base_patches(monkeypatch, flow_domain: FlowDomain, scenario, graph) -> None:
    monkeypatch.setattr(
        flow_domain.global_domain,
        "_prepare_runtime_request",
        lambda incoming: incoming,
    )
    monkeypatch.setattr(
        flow_domain.flow_config_provider,
        "get_snapshot",
        lambda: asyncio.sleep(
            0,
            result=SimpleNamespace(
                source="static",
                updated_at=None,
                stale=False,
                flow_policy=FlowConfigSchema(),
                scenario_packs=[scenario],
                flow_skill_descriptors=[],
            ),
        ),
    )
    monkeypatch.setattr(
        flow_domain.scenario_resolver, "resolve", lambda **_: [scenario]
    )
    monkeypatch.setattr(
        flow_domain.hyperedge_planner, "build_execution_graph", lambda **_: graph
    )
    monkeypatch.setattr(
        flow_domain.skill_hub,
        "list_by_agent",
        lambda **_: SkillMountPlan(
            allowed_tools=["wenshu_agent"],
            tool_context_overlay={},
            authorized_skills=[],
            denied_skills=[],
        ),
    )


def test_flow_node_spans_topology_verdict_and_repair(monkeypatch) -> None:
    provider, exporter = _install_test_tracer(monkeypatch)

    request = FlowChatRequest(query="订单确认收入")
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    scenario = ScenarioPackSchema(
        scenario_id="order_revenue_confirmation",
        display_name="订单确认收入",
        domain="finance_supply_chain",
        required_agents=["Operations"],
        auth_scopes=["scenario:order_revenue_confirmation:read"],
    )
    graph = BusinessExecutionGraphSchema(
        graph_id="g1",
        scenario_ids=[scenario.scenario_id],
        nodes=[
            GraphNodeSchema(
                node_id="node_1",
                scenario_id=scenario.scenario_id,
                agent_code="Operations",
                goal="校验证据",
                evidence_contract=["delivery_record"],
            )
        ],
        edges=[],
    )
    _base_patches(monkeypatch, flow_domain, scenario, graph)

    captured_inner: dict[str, Any] = {}

    async def fake_run_graph_node(**kwargs: Any):
        node = kwargs["node"]
        # Acceptance: current trace context must be valid inside the node
        # (regression for the direct span-attach P0 defect).
        captured_inner.setdefault(node.node_id, current_trace_context())
        # Inner work spans must become children of the node span.
        inner_tracer = provider.get_tracer("flow-test-inner")
        with inner_tracer.start_as_current_span(f"inner.{node.node_id}"):
            pass
        if "_repair_" in node.node_id:
            content = "delivery_record"  # repair node passes
        else:
            content = "缺少证据"  # first node -> uncertain -> repair
        return (
            AgentResult(
                success=True,
                name=node.agent_code,
                content=content,
                data_source={"source": "test"},
            ),
            ["wenshu_agent"],
        )

    async def fake_summarize(*_: Any, **__: Any):
        async def _chunks() -> AsyncGenerator[str, None]:
            yield "summary ok"

        return _chunks()

    monkeypatch.setattr(flow_domain, "_run_graph_node", fake_run_graph_node)
    monkeypatch.setattr(flow_domain.global_domain, "summarize", fake_summarize)

    events = asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))

    # SSE phase contract unchanged (flow_* events preserved)
    phases = [
        event.data.get("phase")
        for event in events
        if event.event == "meta" and isinstance(event.data, dict)
    ]
    assert "flow_node_started" in phases
    assert "flow_node_result" in phases
    assert "flow_repair_applied" in phases

    # span modelling: one CHAIN span per node, repair parented to failed node
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert "flow.node.node_1" in spans
    repair_span_name = next(
        name for name in spans if name.startswith("flow.node.node_1_repair_")
    )
    node_span = spans["flow.node.node_1"]
    repair_span = spans[repair_span_name]
    assert repair_span.parent.span_id == node_span.context.span_id

    assert (
        node_span.attributes["openinference.span.kind"] == "CHAIN"
        and node_span.attributes["map.flow.node_id"] == "node_1"
        and node_span.attributes["map.flow.agent_code"] == "Operations"
    )

    event_names = [event.name for event in node_span.events]
    assert "flow.verdict" in event_names
    assert "flow.repair_applied" in event_names
    verdict_event = next(e for e in node_span.events if e.name == "flow.verdict")
    assert verdict_event.attributes["map.flow.verdict"] == "uncertain"

    # Inner context resolves to the node span (valid ids, matching span id)
    inner_ctx = captured_inner["node_1"]
    assert inner_ctx["trace_id"] == format(node_span.context.trace_id, "032x")
    assert inner_ctx["span_id"] == format(node_span.context.span_id, "016x")
    inner_span = spans["inner.node_1"]
    assert inner_span.parent.span_id == node_span.context.span_id
    assert node_span.status.status_code == StatusCode.UNSET


def test_flow_node_forwards_dispatch_config_engine(monkeypatch) -> None:
    request = FlowChatRequest(
        query="订单确认收入",
        dispatch_config=AgentDispatchConfigSchema(engine="agentscope"),
    )
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    captured: dict[str, Any] = {}

    async def fake_run_single_agent(name, agent_request, **kwargs):
        captured["name"] = name
        captured["engine"] = kwargs.get("engine")
        return AgentResult(success=True, name=name, content="ok")

    monkeypatch.setattr(
        flow_domain.global_domain.agent_dispatcher,
        "run_single_agent",
        fake_run_single_agent,
    )

    scenario = ScenarioPackSchema(
        scenario_id="order_revenue_confirmation",
        display_name="订单确认收入",
        domain="finance_supply_chain",
        required_agents=["Operations"],
    )
    node = GraphNodeSchema(
        node_id="node_1",
        scenario_id=scenario.scenario_id,
        agent_code="Operations",
        goal="校验证据",
        evidence_contract=["delivery_record"],
    )
    mount_plan = SkillMountPlan(
        allowed_tools=["wenshu_agent"],
        tool_context_overlay={},
        authorized_skills=[],
        denied_skills=[],
    )

    result, tool_names = asyncio.run(
        flow_domain._run_graph_node(
            request=request,
            node=node,
            mount_plan=mount_plan,
            scenarios=[scenario],
        )
    )

    assert captured["name"] == "Operations"
    assert captured["engine"] == "agentscope"
    assert result.success is True
    assert tool_names == ["wenshu_agent"]


def test_flow_node_engine_defaults_to_none_without_dispatch_config(monkeypatch) -> None:
    request = FlowChatRequest(query="订单确认收入")
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    captured: dict[str, Any] = {}

    async def fake_run_single_agent(name, agent_request, **kwargs):
        captured["engine"] = kwargs.get("engine")
        return AgentResult(success=True, name=name, content="ok")

    monkeypatch.setattr(
        flow_domain.global_domain.agent_dispatcher,
        "run_single_agent",
        fake_run_single_agent,
    )

    scenario = ScenarioPackSchema(
        scenario_id="s1",
        display_name="s1",
        domain="d",
        required_agents=["Operations"],
    )
    node = GraphNodeSchema(
        node_id="node_1",
        scenario_id="s1",
        agent_code="Operations",
        goal="g",
        evidence_contract=[],
    )
    mount_plan = SkillMountPlan(
        allowed_tools=[],
        tool_context_overlay={},
        authorized_skills=[],
        denied_skills=[],
    )

    asyncio.run(
        flow_domain._run_graph_node(
            request=request,
            node=node,
            mount_plan=mount_plan,
            scenarios=[scenario],
        )
    )

    # None lets AgentRuntime fall back to env var / legacy default
    assert captured["engine"] is None
    assert FlowDomainStreamEvent is not None  # schema import guard


def _pipeline_scaffold(monkeypatch, flow_domain: FlowDomain, scenario, graph):
    _base_patches(monkeypatch, flow_domain, scenario, graph)

    async def fake_summarize(*_: Any, **__: Any):
        async def _chunks() -> AsyncGenerator[str, None]:
            yield "summary ok"

        return _chunks()

    monkeypatch.setattr(flow_domain.global_domain, "summarize", fake_summarize)


def test_flow_node_exception_sets_error_status(monkeypatch) -> None:
    _, exporter = _install_test_tracer(monkeypatch)

    request = FlowChatRequest(query="订单确认收入")
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    scenario = ScenarioPackSchema(
        scenario_id="s1",
        display_name="s1",
        domain="d",
        required_agents=["Operations"],
    )
    graph = BusinessExecutionGraphSchema(
        graph_id="g1",
        scenario_ids=["s1"],
        nodes=[
            GraphNodeSchema(
                node_id="node_1",
                scenario_id="s1",
                agent_code="Operations",
                goal="g",
                evidence_contract=["evidence"],
            )
        ],
        edges=[],
    )
    _pipeline_scaffold(monkeypatch, flow_domain, scenario, graph)

    async def exploding_node(**_: Any):
        raise RuntimeError("boom")

    async def no_fallback(**_: Any):
        if False:
            yield None

    monkeypatch.setattr(flow_domain, "_run_graph_node", exploding_node)
    monkeypatch.setattr(flow_domain, "_fallback_to_global_domain", no_fallback)

    asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    node_span = spans["flow.node.node_1"]
    assert node_span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in node_span.events)


def test_flow_node_multiple_dependencies_use_parent_and_links(monkeypatch) -> None:
    _, exporter = _install_test_tracer(monkeypatch)

    request = FlowChatRequest(query="多依赖汇聚")
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    scenario = ScenarioPackSchema(
        scenario_id="s1",
        display_name="s1",
        domain="d",
        required_agents=["Operations"],
    )
    graph = BusinessExecutionGraphSchema(
        graph_id="g1",
        scenario_ids=["s1"],
        nodes=[
            GraphNodeSchema(
                node_id="node_a",
                scenario_id="s1",
                agent_code="Operations",
                goal="g",
                evidence_contract=["evidence"],
            ),
            GraphNodeSchema(
                node_id="node_b",
                scenario_id="s1",
                agent_code="Operations",
                goal="g",
                evidence_contract=["evidence"],
            ),
            GraphNodeSchema(
                node_id="node_c",
                scenario_id="s1",
                agent_code="Operations",
                goal="g",
                evidence_contract=["evidence"],
                depends_on=["node_a", "node_b"],
            ),
        ],
        edges=[],
    )
    _pipeline_scaffold(monkeypatch, flow_domain, scenario, graph)

    async def passing_node(**kwargs: Any):
        node = kwargs["node"]
        return (
            AgentResult(
                success=True,
                name=node.agent_code,
                content="evidence",
                data_source={"source": "test"},
            ),
            ["wenshu_agent"],
        )

    monkeypatch.setattr(flow_domain, "_run_graph_node", passing_node)

    asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert {"flow.node.node_a", "flow.node.node_b", "flow.node.node_c"} <= set(spans)
    join_span = spans["flow.node.node_c"]
    # First dependency is the parent, the second one is expressed as a link.
    assert join_span.parent.span_id == spans["flow.node.node_a"].context.span_id
    link_contexts = [link.context.span_id for link in join_span.links]
    assert link_contexts == [spans["flow.node.node_b"].context.span_id]
