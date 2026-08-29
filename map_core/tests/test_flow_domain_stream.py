import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

from map_core.schema.flow_domain_schema import (
    BusinessExecutionGraphSchema,
    FlowChatRequest,
    FlowConfigSchema,
    GraphNodeSchema,
    ScenarioPackSchema,
)
from map_core.schema.global_domain_schema import GlobalDomainStreamEvent
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


def test_flow_domain_success_path_with_expected_phases(monkeypatch) -> None:
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

    monkeypatch.setattr(flow_domain.global_domain, "_prepare_runtime_request", lambda incoming: incoming)
    monkeypatch.setattr(
        flow_domain.flow_config_provider,
        "get_snapshot",
        lambda *, snapshot_id=None, expected_digest=None: asyncio.sleep(
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
    monkeypatch.setattr(flow_domain.scenario_resolver, "resolve", lambda **_: [scenario])
    monkeypatch.setattr(flow_domain.hyperedge_planner, "build_execution_graph", lambda **_: graph)
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

    async def fake_run_graph_node(**_: Any):
        return (
            AgentResult(
                success=True,
                name="Operations",
                content="delivery_record",
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
    phases = [
        event.data.get("phase")
        for event in events
        if event.event == "meta" and isinstance(event.data, dict)
    ]

    assert "flow_mode_initialized" in phases
    assert "scenario_resolved" in phases
    assert "flow_graph_built" in phases
    assert "flow_node_started" in phases
    assert "flow_node_result" in phases

    done_events = [event for event in events if event.event == "done"]
    assert done_events
    assert done_events[0].data.get("content") == "summary ok"


def test_flow_domain_fallback_to_global_domain_when_no_scenario(monkeypatch) -> None:
    request = FlowChatRequest(query="普通问答")
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    monkeypatch.setattr(flow_domain.global_domain, "_prepare_runtime_request", lambda incoming: incoming)
    monkeypatch.setattr(
        flow_domain.flow_config_provider,
        "get_snapshot",
        lambda *, snapshot_id=None, expected_digest=None: asyncio.sleep(
            0,
            result=SimpleNamespace(
                source="static",
                updated_at=None,
                stale=False,
                flow_policy=FlowConfigSchema(),
                scenario_packs=[],
                flow_skill_descriptors=[],
            ),
        ),
    )
    monkeypatch.setattr(flow_domain.scenario_resolver, "resolve", lambda **_: [])

    async def fake_global_stream(_: Any):
        yield GlobalDomainStreamEvent(event="meta", data={"phase": "scene_selected"})
        yield GlobalDomainStreamEvent(
            event="done",
            data={
                "content": "fallback done",
                "attachment_results": None,
                "tool_extra_results": None,
                "meta": {},
                "request_id": "r1",
                "state_id": "s1",
            },
        )

    monkeypatch.setattr(flow_domain.global_domain, "pipeline_stream", fake_global_stream)

    events = asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))
    phases = [
        event.data.get("phase")
        for event in events
        if event.event == "meta" and isinstance(event.data, dict)
    ]
    assert "flow_fallback" in phases

    done_events = [event for event in events if event.event == "done"]
    assert done_events
    assert done_events[0].data.get("content") == "fallback done"


def test_flow_domain_hard_fail_when_fallback_disabled(monkeypatch) -> None:
    request = FlowChatRequest(
        query="普通问答",
        flow_config=FlowConfigSchema(fallback_to_global=False),
    )
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    monkeypatch.setattr(flow_domain.global_domain, "_prepare_runtime_request", lambda incoming: incoming)
    monkeypatch.setattr(
        flow_domain.flow_config_provider,
        "get_snapshot",
        lambda *, snapshot_id=None, expected_digest=None: asyncio.sleep(
            0,
            result=SimpleNamespace(
                source="static",
                updated_at=None,
                stale=False,
                flow_policy=request.flow_config,
                scenario_packs=[],
                flow_skill_descriptors=[],
            ),
        ),
    )
    monkeypatch.setattr(flow_domain.scenario_resolver, "resolve", lambda **_: [])

    events = asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))
    error_events = [event for event in events if event.event == "error"]
    done_events = [event for event in events if event.event == "done"]
    assert error_events
    assert done_events
    assert done_events[0].data.get("meta", {}).get("fallback") is False
