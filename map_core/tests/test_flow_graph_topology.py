import asyncio
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

from map_core.schema.flow_domain_schema import (
    BusinessExecutionGraphSchema,
    FlowChatRequest,
    FlowConfigSchema,
    GraphEdgeSchema,
    GraphNodeSchema,
    ScenarioPackSchema,
)
from map_core.service.agent.base import AgentResult
from map_core.service.execution_event import set_run_context
from map_core.service.flow_domain import FlowDomain
from map_core.service.skill_hub import SkillMountPlan


class _DummyStateStore:
    async def record_event(self, **_: Any) -> None:
        return None


async def _collect_events(stream: AsyncGenerator[Any, None]) -> list[Any]:
    rows: list[Any] = []
    with set_run_context(run_id=uuid.uuid4()):
        async for item in stream:
            rows.append(item)
    return rows


def test_flow_graph_runs_in_dependency_order(monkeypatch) -> None:
    request = FlowChatRequest(query="跨域串行")
    flow_domain = FlowDomain(request=request)
    flow_domain.global_domain.state_store = _DummyStateStore()

    scenario = ScenarioPackSchema(
        scenario_id="cross_domain_serial_analysis",
        display_name="跨域串行分析",
        domain="general",
        required_agents=["Operations", "General_Assistant"],
    )
    graph = BusinessExecutionGraphSchema(
        graph_id="g-topo",
        scenario_ids=[scenario.scenario_id],
        nodes=[
            GraphNodeSchema(
                node_id="n1",
                scenario_id=scenario.scenario_id,
                agent_code="Operations",
                goal="step1",
            ),
            GraphNodeSchema(
                node_id="n2",
                scenario_id=scenario.scenario_id,
                agent_code="General_Assistant",
                goal="step2",
                depends_on=["n1"],
            ),
        ],
        edges=[
            GraphEdgeSchema(**{"from": "n1", "to": "n2", "condition": "pass"}),
        ],
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

    run_order: list[str] = []

    async def fake_run_graph_node(**kwargs: Any):
        node = kwargs["node"]
        run_order.append(node.node_id)
        return (
            AgentResult(
                success=True,
                name=node.agent_code,
                content=f"{node.node_id}:ok",
                data_source={"source": "test"},
            ),
            ["wenshu_agent"],
        )

    async def fake_summarize(*_: Any, **__: Any):
        async def _chunks() -> AsyncGenerator[str, None]:
            yield "ok"

        return _chunks()

    monkeypatch.setattr(flow_domain, "_run_graph_node", fake_run_graph_node)
    monkeypatch.setattr(flow_domain.global_domain, "summarize", fake_summarize)

    asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))
    assert run_order == ["n1", "n2"]
