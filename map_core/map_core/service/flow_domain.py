from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator
from zoneinfo import ZoneInfo

from fastapi import Request
from loguru import logger
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import StatusCode as OtelStatusCode

from ..observability import get_tracer
from ..schema.flow_domain_schema import (
    BusinessExecutionGraphSchema,
    FlowChatRequest,
    FlowConfigSchema,
    FlowDomainStreamEvent,
    GraphEdgeSchema,
    GraphNodeSchema,
    NodeExecutionResultSchema,
    StepVerdictSchema,
)
from ..schema.global_domain_schema import (
    GlobalDomainChatSchema,
    GlobalDomainStreamEvent,
)
from .agent.agent_mapping import SCENE_AGENT_CONFIGS
from .agent.base import AgentRequest, AgentResult
from .agent_case_miner import AgentCaseMiner
from .flow_config_provider import FlowConfigProvider
from .global_domain import GlobalDomain
from .global_domain_helpers import (
    build_dispatch_token_meta,
    normalize_attachment_results,
    normalize_tool_extra_results,
    serialize_attachment_results,
    serialize_tool_extra_results,
    stream_event_data_as_dict,
)
from .hyperedge_planner import HyperedgePlanner
from .scenario_hub import ScenarioHub
from .scenario_resolver import ScenarioResolver
from .skill_hub import SkillHub, SkillMountPlan
from .state_store import fire_and_forget, safe_serialize

_flow_tracer = get_tracer(__name__)


@dataclass
class GraphRuntimeState:
    """In-memory execution-graph state for one request lifecycle.

    Inlined from ``service/business_execution_graph_store.py`` (Step 1
    deletion test: the former static facade had exactly one caller and its
    two operations read/append one caller-owned object, so the facade hid
    no decision and the complexity moves locally into ``FlowDomain``).
    """

    graph: BusinessExecutionGraphSchema
    node_results: dict[str, NodeExecutionResultSchema] = field(default_factory=dict)
    verdicts: dict[str, StepVerdictSchema] = field(default_factory=dict)

    def record(
        self,
        *,
        node_id: str,
        node_result: NodeExecutionResultSchema,
        verdict: StepVerdictSchema,
    ) -> None:
        self.node_results[node_id] = node_result
        self.verdicts[node_id] = verdict

    def is_finished(self, node_id: str) -> bool:
        return node_id in self.verdicts

    def is_passed(self, node_id: str) -> bool:
        verdict = self.verdicts.get(node_id)
        return bool(verdict and verdict.verdict == "pass")

    def as_dict(self) -> dict[str, object]:
        return {
            "node_results": {
                key: value.model_dump()
                for key, value in self.node_results.items()
            },
            "verdicts": {
                key: value.model_dump()
                for key, value in self.verdicts.items()
            },
            "graph": self.graph.model_dump(by_alias=True),
        }


class FlowDomain:
    """Flow-mode orchestrator with ScenarioHub + SkillHub and fallback support."""

    def __init__(
        self,
        request: FlowChatRequest | None = None,
        http_request: Request | None = None,
    ) -> None:
        self.global_domain = GlobalDomain(request=request, http_request=http_request)
        self.scenario_hub = ScenarioHub()
        self.skill_hub = SkillHub()
        self.scenario_resolver = ScenarioResolver()
        self.hyperedge_planner = HyperedgePlanner()
        self.flow_config_provider = FlowConfigProvider.instance()
        self.agent_case_miner = AgentCaseMiner()

    @property
    def request_id(self) -> str:
        return self.global_domain.request_id

    @property
    def state_id(self) -> str:
        return self.global_domain.state_id

    @property
    def state_store(self):
        return self.global_domain.state_store

    @property
    def attachment_collector(self):
        return self.global_domain.attachment_collector

    @property
    def tool_extra_result_collector(self):
        return self.global_domain.tool_extra_result_collector

    def _to_global_domain_request(self, request: FlowChatRequest) -> GlobalDomainChatSchema:
        payload = request.model_dump(exclude={"flow_config"})
        return GlobalDomainChatSchema.model_validate(payload)

    @staticmethod
    def _default_flow_config() -> FlowConfigSchema:
        return FlowConfigSchema()

    def _resolve_effective_flow_config(
        self,
        *,
        request: FlowChatRequest,
        remote_flow_policy: FlowConfigSchema,
    ) -> FlowConfigSchema:
        request_config = request.flow_config
        if request_config.model_dump() == self._default_flow_config().model_dump():
            return remote_flow_policy.model_copy(deep=True)
        merged = remote_flow_policy.model_copy(deep=True)
        merged.scenario_policy = request_config.scenario_policy
        merged.skill_policy = request_config.skill_policy
        merged.max_node_budget = request_config.max_node_budget
        merged.fallback_to_global = request_config.fallback_to_global
        return merged

    def _should_fallback_to_global(self, request: FlowChatRequest) -> bool:
        return bool(request.flow_config.fallback_to_global)

    async def _emit_flow_hard_fail(
        self,
        *,
        reason: str,
        message: str,
    ) -> AsyncGenerator[FlowDomainStreamEvent, None]:
        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="flow.hard_fail",
                payload={
                    "request_id": self.request_id,
                    "state_id": self.state_id,
                    "reason": reason,
                    "message": message,
                },
            )
        )
        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="request.end",
                payload={
                    "request_id": self.request_id,
                    "session_id": self.global_domain.session_id,
                    "workspace_id": self.global_domain.workspace_id,
                    "status": "failed",
                    "error": message,
                },
            )
        )
        yield FlowDomainStreamEvent(
            event="error",
            data={
                "error": message,
                "mode": "flow",
                "fallback": False,
                "reason": reason,
            },
        )
        yield FlowDomainStreamEvent(
            event="done",
            data={
                "content": message,
                "meta": {
                    "mode": "flow",
                    "fallback": False,
                    "reason": reason,
                },
                "request_id": self.request_id,
                "state_id": self.state_id,
                "finished": True,
            },
        )

    async def _fallback_to_global_domain(
        self,
        *,
        request: FlowChatRequest,
        reason: str,
        message: str | None = None,
    ) -> AsyncGenerator[FlowDomainStreamEvent, None]:
        if not self._should_fallback_to_global(request):
            async for event in self._emit_flow_hard_fail(
                reason=reason,
                message=message or "心流模式未命中可执行路径，且已禁用回退全域模式。",
            ):
                yield event
            return

        yield FlowDomainStreamEvent(
            event="meta",
            data={
                "phase": "flow_fallback",
                "reason": reason,
            },
        )
        fallback_request = self._to_global_domain_request(request)
        async for event in self.global_domain.pipeline_stream(fallback_request):
            if isinstance(event, GlobalDomainStreamEvent):
                yield FlowDomainStreamEvent(
                    event=event.event,
                    data=stream_event_data_as_dict(event),
                )

    @staticmethod
    def _evaluate_node(
        *,
        node: GraphNodeSchema,
        result: AgentResult,
        executor_names: list[str],
    ) -> tuple[NodeExecutionResultSchema, StepVerdictSchema]:
        content = str(result.content or "")
        lowered_content = content.lower()
        matched_evidence = [
            evidence
            for evidence in node.evidence_contract
            if evidence.lower() in lowered_content
        ]
        missing_evidence = [
            evidence
            for evidence in node.evidence_contract
            if evidence not in matched_evidence
        ]

        if isinstance(result.error, str) and "tool_forbidden" in result.error:
            verdict_name = "fail"
            node_status = "failed"
            score = 0.0
            execution_status = "denied"
        elif not result.success:
            verdict_name = "fail"
            node_status = "failed"
            score = 0.1
            execution_status = "failed"
        elif missing_evidence:
            verdict_name = "uncertain"
            node_status = "uncertain"
            score = 0.45
            execution_status = "uncertain"
        else:
            verdict_name = "pass"
            node_status = "passed"
            score = 0.9
            execution_status = "success"

        node_result = NodeExecutionResultSchema(
            node_id=node.node_id,
            agent_code=node.agent_code,
            executor_type="tool",
            executor_names=executor_names,
            status=execution_status,
            content=content,
            evidence_refs=[],
            confidence=score,
            missing_evidence=missing_evidence,
            recommended_next_actions=[],
        )
        verdict = StepVerdictSchema(
            node_id=node.node_id,
            verdict=verdict_name,
            score=score,
            matched_evidence=matched_evidence,
            missing_evidence=missing_evidence,
            issues=[result.error] if result.error else [],
            repair_candidates=[],
        )
        node.status = node_status
        return node_result, verdict

    @staticmethod
    def _build_authorization_map(
        mount_plan: SkillMountPlan,
        agent_code: str,
    ) -> dict[str, dict[str, Any]]:
        auth_map: dict[str, dict[str, Any]] = {}
        for item in mount_plan.authorized_skills:
            tool_name = item.get("tool_name")
            if not tool_name:
                continue
            auth_map[str(tool_name)] = {
                "allowed": True,
                "reason": "authorized",
                "agent_code": agent_code,
                "meta": item,
            }
        for item in mount_plan.denied_skills:
            tool_name = item.get("tool_name")
            if not tool_name:
                continue
            if str(tool_name) in auth_map:
                continue
            auth_map[str(tool_name)] = {
                "allowed": False,
                "reason": item.get("reason") or "policy_denied",
                "agent_code": agent_code,
                "meta": item,
            }
        return auth_map

    def _build_runtime_request(
        self,
        *,
        request: FlowChatRequest,
        node: GraphNodeSchema,
        mount_plan: SkillMountPlan,
        scenarios: list[Any],
    ) -> AgentRequest:
        extra = self.global_domain._build_agent_extra(request)
        extra["flow_mode"] = "heartflow"
        extra["skill_policy_runtime_auth_check"] = (
            request.flow_config.skill_policy.runtime_auth_check
        )
        extra["skill_policy_allowed_tools"] = list(mount_plan.allowed_tools)
        extra["skill_policy_allowed_tools_by_agent"] = {
            node.agent_code: list(mount_plan.allowed_tools)
        }
        extra["skill_policy_authorization_map"] = self._build_authorization_map(
            mount_plan,
            node.agent_code,
        )
        extra["skill_policy_context"] = {
            "user_id": self.global_domain.x_userid,
            "tenant": self.global_domain.x_username,
            "scenario_ids": [scenario.scenario_id for scenario in scenarios],
        }
        if request.tool_context is not None:
            extra["tool_context"] = request.tool_context
        return AgentRequest(
            query=request.query,
            original_query=self.global_domain._resolve_original_query(request),
            staff_code=self.global_domain.staff_code,
            scene_result=None,
            history=request.history,
            extra=extra,
            state_store=self.state_store,
            state_id=self.state_id,
        )

    async def _run_graph_node(
        self,
        *,
        request: FlowChatRequest,
        node: GraphNodeSchema,
        mount_plan: SkillMountPlan,
        scenarios: list[Any],
    ) -> tuple[AgentResult, list[str]]:
        base_config = SCENE_AGENT_CONFIGS.get(node.agent_code)
        if base_config is None:
            return (
                AgentResult(
                    success=False,
                    name=node.agent_code,
                    content="",
                    error=f"unknown scene agent: {node.agent_code}",
                    data_source={"source": "flow_domain"},
                ),
                [],
            )

        runtime_tool_names = list(mount_plan.allowed_tools) or list(base_config.tool_names)
        runtime_config = base_config.model_copy(update={"tool_names": runtime_tool_names})
        agent_request = self._build_runtime_request(
            request=request,
            node=node,
            mount_plan=mount_plan,
            scenarios=scenarios,
        )

        result = await self.global_domain.agent_dispatcher.run_single_agent(
            node.agent_code,
            agent_request,
            config=runtime_config,
            state_store=self.state_store,
            state_id=self.state_id,
            tool_context=mount_plan.tool_context_overlay,
            engine=getattr(getattr(request, "dispatch_config", None), "engine", None),
        )
        return result, runtime_tool_names

    @staticmethod
    def _incoming_edges(
        state: GraphRuntimeState,
        node_id: str,
    ) -> list[GraphEdgeSchema]:
        return [edge for edge in state.graph.edges if edge.to_node == node_id]

    def _is_edge_condition_satisfied(
        self,
        state: GraphRuntimeState,
        edge: GraphEdgeSchema,
    ) -> bool:
        from_verdict = state.verdicts.get(edge.from_node)
        if from_verdict is None:
            return False

        condition = (edge.condition or "pass").strip().lower()
        if condition in {"", "pass", "success"}:
            return from_verdict.verdict == "pass"
        if condition in {"repair", "fail_or_uncertain"}:
            return from_verdict.verdict in {"fail", "uncertain"}
        if condition == "always":
            return True
        if condition.startswith("verdict=="):
            expected = condition.split("==", 1)[1].strip()
            return from_verdict.verdict == expected
        return from_verdict.verdict == "pass"

    def _is_node_ready(
        self,
        state: GraphRuntimeState,
        node: GraphNodeSchema,
    ) -> bool:
        if state.is_finished(node.node_id):
            return False

        for dependency_node_id in node.depends_on:
            if not state.is_finished(dependency_node_id):
                return False

        incoming = self._incoming_edges(state, node.node_id)
        if not incoming:
            return True

        return all(self._is_edge_condition_satisfied(state, edge) for edge in incoming)

    def _next_ready_nodes(self, state: GraphRuntimeState) -> list[GraphNodeSchema]:
        return [
            node
            for node in state.graph.nodes
            if node.status == "pending" and self._is_node_ready(state, node)
        ]

    async def pipeline_stream(
        self,
        request: FlowChatRequest,
    ) -> AsyncGenerator[FlowDomainStreamEvent, None]:
        request_start_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
        request = self.global_domain._prepare_runtime_request(request)

        fire_and_forget(
            self.state_store.record_event(
                state_id=self.state_id,
                event_type="request.start",
                payload={
                    "request_id": self.request_id,
                    "state_id": self.state_id,
                    "mode": "flow",
                    "query": request.query,
                },
            )
        )

        yield FlowDomainStreamEvent(
            event="start",
            data={
                "request_id": self.request_id,
                "state_id": self.state_id,
                "flow_mode": "heartflow",
            },
        )

        try:
            snapshot = await self.flow_config_provider.get_snapshot()
            self.scenario_hub.load_external_scenarios(snapshot.scenario_packs)
            self.skill_hub.load_external_skills(snapshot.flow_skill_descriptors)

            effective_flow_config = self._resolve_effective_flow_config(
                request=request,
                remote_flow_policy=snapshot.flow_policy,
            )
            request.flow_config = effective_flow_config

            yield FlowDomainStreamEvent(
                event="meta",
                data={
                    "phase": "flow_mode_initialized",
                    "flow_config": safe_serialize(request.flow_config.model_dump()),
                    "config_snapshot": {
                        "source": snapshot.source,
                        "updated_at": snapshot.updated_at,
                        "stale": snapshot.stale,
                    },
                },
            )
            policy_hit = {
                "config_source": snapshot.source,
                "snapshot_updated_at": snapshot.updated_at,
                "snapshot_stale": snapshot.stale,
                "runtime_override": request.flow_config.model_dump()
                != snapshot.flow_policy.model_dump(),
                "effective_policy": safe_serialize(request.flow_config.model_dump()),
            }
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="flow.policy_hit",
                    payload={
                        "request_id": self.request_id,
                        "state_id": self.state_id,
                        **policy_hit,
                    },
                )
            )
            yield FlowDomainStreamEvent(
                event="meta",
                data={
                    "phase": "flow_policy_hit",
                    **policy_hit,
                },
            )

            scenarios = self.scenario_resolver.resolve(
                query=request.query,
                scenario_policy=request.flow_config.scenario_policy,
                tool_context=request.tool_context,
                scenario_hub=self.scenario_hub,
            )
            yield FlowDomainStreamEvent(
                event="meta",
                data={
                    "phase": "scenario_resolved",
                    "matched_scenarios": [
                        {
                            "scenario_id": scenario.scenario_id,
                            "display_name": scenario.display_name,
                            "domain": scenario.domain,
                        }
                        for scenario in scenarios
                    ],
                },
            )
            if not scenarios:
                async for event in self._fallback_to_global_domain(
                    request=request,
                    reason="no_matched_scenario",
                    message="心流模式未命中可执行场景。",
                ):
                    yield event
                return

            graph = self.hyperedge_planner.build_execution_graph(
                scenarios=scenarios,
                max_node_budget=request.flow_config.max_node_budget,
                scenario_hub=self.scenario_hub,
            )
            if not graph.nodes:
                async for event in self._fallback_to_global_domain(
                    request=request,
                    reason="empty_execution_graph",
                    message="已命中心流场景，但执行图为空。",
                ):
                    yield event
                return

            state = GraphRuntimeState(graph=graph)

            yield FlowDomainStreamEvent(
                event="meta",
                data={
                    "phase": "flow_graph_built",
                    "graph": safe_serialize(graph.model_dump(by_alias=True)),
                },
            )

            repair_budget = request.flow_config.scenario_policy.max_graph_cycles
            repair_count = 0
            dispatch_results: list[AgentResult] = []
            node_results: list[NodeExecutionResultSchema] = []
            verdicts: list[StepVerdictSchema] = []
            node_spans: dict[str, Any] = {}

            while True:
                ready_nodes = self._next_ready_nodes(state)
                if not ready_nodes:
                    break

                node = ready_nodes[0]
                node.status = "running"
                yield FlowDomainStreamEvent(
                    event="meta",
                    data={
                        "phase": "flow_node_started",
                        "node": safe_serialize(node.model_dump()),
                    },
                )

                # First dependency acts as the span parent; remaining
                # dependencies are expressed as OTel links.
                dependency_spans = [
                    node_spans[dependency_id]
                    for dependency_id in (node.depends_on or [])
                    if dependency_id in node_spans
                ]
                node_span_parent = None
                node_span_links = None
                if dependency_spans:
                    node_span_parent = otel_trace.set_span_in_context(
                        dependency_spans[0]
                    )
                    if len(dependency_spans) > 1:
                        node_span_links = [
                            otel_trace.Link(item.get_span_context())
                            for item in dependency_spans[1:]
                        ]
                node_span = _flow_tracer.start_span(
                    f"flow.node.{node.node_id}",
                    context=node_span_parent,
                    links=node_span_links,
                    attributes={
                        "openinference.span.kind": "CHAIN",
                        "map.flow.node_id": node.node_id,
                        "map.flow.agent_code": node.agent_code,
                        "map.flow.scenario_id": str(node.scenario_id or ""),
                        "map.flow.is_repair_node": "_repair_" in node.node_id,
                    },
                )
                node_span_context = otel_trace.set_span_in_context(node_span)
                node_span_token = otel_context.attach(node_span_context)
                try:
                    mount_plan = self.skill_hub.list_by_agent(
                        agent_code=node.agent_code,
                        scenarios=scenarios,
                        base_tool_context=request.tool_context,
                        user_id=self.global_domain.x_userid,
                        tenant=self.global_domain.x_username,
                    )
                    node.allowed_capabilities = list(mount_plan.allowed_tools)
                    fire_and_forget(
                        self.state_store.record_event(
                            state_id=self.state_id,
                            event_type="flow.skill_authorization",
                            payload={
                                "request_id": self.request_id,
                                "state_id": self.state_id,
                                "node_id": node.node_id,
                                "agent_code": node.agent_code,
                                "authorized_skills": safe_serialize(
                                    mount_plan.authorized_skills
                                ),
                                "denied_skills": safe_serialize(mount_plan.denied_skills),
                            },
                        )
                    )

                    yield FlowDomainStreamEvent(
                        event="meta",
                        data={
                            "phase": "skill_authorization",
                            "node_id": node.node_id,
                            "agent_code": node.agent_code,
                            "authorized_skills": safe_serialize(mount_plan.authorized_skills),
                            "denied_skills": safe_serialize(mount_plan.denied_skills),
                        },
                    )

                    result, runtime_tool_names = await self._run_graph_node(
                        request=request,
                        node=node,
                        mount_plan=mount_plan,
                        scenarios=scenarios,
                    )
                    dispatch_results.append(result)

                    node_result, verdict = self._evaluate_node(
                        node=node,
                        result=result,
                        executor_names=runtime_tool_names,
                    )
                    repair_candidates = self.scenario_hub.suggest_repair(
                        node=node,
                        verdict=verdict,
                    )
                    verdict.repair_candidates = repair_candidates

                    node_span.add_event(
                        "flow.verdict",
                        {
                            "map.flow.node_id": node.node_id,
                            "map.flow.verdict": verdict.verdict,
                            "map.flow.repair_candidate_count": len(repair_candidates),
                        },
                    )

                    state.record(
                        node_id=node.node_id,
                        node_result=node_result,
                        verdict=verdict,
                    )
                    node_results.append(node_result)
                    verdicts.append(verdict)

                    yield FlowDomainStreamEvent(
                        event="meta",
                        data={
                            "phase": "flow_node_result",
                            "node_result": safe_serialize(node_result.model_dump()),
                            "step_verdict": safe_serialize(verdict.model_dump()),
                            "graph_state": safe_serialize(state.as_dict()),
                        },
                    )

                    if verdict.verdict != "pass":
                        if (
                            request.flow_config.scenario_policy.allow_graph_repair
                            and repair_count < repair_budget
                            and repair_candidates
                        ):
                            candidate = repair_candidates[0]
                            repair_count += 1
                            repair_node = GraphNodeSchema(
                                node_id=f"{node.node_id}_repair_{repair_count}",
                                scenario_id=node.scenario_id,
                                agent_code=candidate.target_agent,
                                goal=candidate.reason,
                                evidence_contract=list(node.evidence_contract),
                                allowed_capabilities=list(node.allowed_capabilities),
                                status="pending",
                                depends_on=[node.node_id],
                            )
                            state.graph.nodes.append(repair_node)
                            state.graph.edges.append(
                                GraphEdgeSchema(
                                    **{
                                        "from": node.node_id,
                                        "to": repair_node.node_id,
                                        "condition": "repair",
                                    }
                                )
                            )
                            node_span.add_event(
                                "flow.repair_applied",
                                {
                                    "map.flow.node_id": node.node_id,
                                    "map.flow.repair_node_id": repair_node.node_id,
                                    "map.flow.repair_target_agent": candidate.target_agent,
                                },
                            )
                            yield FlowDomainStreamEvent(
                                event="meta",
                                data={
                                    "phase": "flow_repair_applied",
                                    "candidate": safe_serialize(candidate.model_dump()),
                                    "repair_node": safe_serialize(repair_node.model_dump()),
                                },
                            )
                except Exception as exc:
                    node_span.record_exception(exc)
                    node_span.set_status(
                        OtelStatusCode.ERROR,
                        f"flow node {node.node_id} failed: {exc}",
                    )
                    raise
                finally:
                    node_spans[node.node_id] = node_span
                    otel_context.detach(node_span_token)
                    node_span.end()

            remaining = [
                node.node_id
                for node in state.graph.nodes
                if not state.is_finished(node.node_id)
            ]
            if remaining:
                for node_id in remaining:
                    node = next((item for item in state.graph.nodes if item.node_id == node_id), None)
                    if node is not None:
                        node.status = "skipped"
                yield FlowDomainStreamEvent(
                    event="meta",
                    data={
                        "phase": "flow_graph_incomplete",
                        "remaining_nodes": remaining,
                        "reason": "dependencies_or_conditions_not_satisfied",
                    },
                )

            summary_stream = await self.global_domain.summarize(
                request=request,
                dispatch_results=dispatch_results,
                stream=True,
            )
            summary_parts: list[str] = []
            async for chunk in summary_stream:
                text = str(chunk)
                if not text:
                    continue
                summary_parts.append(text)
                yield FlowDomainStreamEvent(
                    event="content_delta",
                    data={"content": text},
                )

            final_content = "".join(summary_parts)
            meta: dict[str, Any] = build_dispatch_token_meta(dispatch_results)
            graph_trace = {
                "scenarios": [scenario.scenario_id for scenario in scenarios],
                "node_results": [item.model_dump() for item in node_results],
                "step_verdicts": [item.model_dump() for item in verdicts],
                "graph": state.graph.model_dump(by_alias=True),
                "repair_count": repair_count,
                "runtime_state": state.as_dict(),
            }
            meta["flow"] = graph_trace

            graph_trace_snapshot = safe_serialize(graph_trace)
            agent_case = self.agent_case_miner.build_agent_case(
                query=request.query,
                scenarios=scenarios,
                graph_trace=graph_trace_snapshot,
                final_content=final_content,
            )
            repair_policy_candidates = self.agent_case_miner.build_repair_policy_candidates(
                scenarios=scenarios,
                graph_trace=graph_trace,
            )
            meta["flow"]["agent_case"] = agent_case
            meta["flow"]["repair_policy_candidates"] = repair_policy_candidates
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="flow.agent_case_candidate",
                    payload={
                        "request_id": self.request_id,
                        "state_id": self.state_id,
                        "agent_case": safe_serialize(agent_case),
                        "repair_policy_candidates": safe_serialize(
                            repair_policy_candidates
                        ),
                    },
                )
            )

            end_ts = datetime.now(ZoneInfo("Asia/Shanghai"))
            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id,
                    event_type="request.end",
                    payload={
                        "request_id": self.request_id,
                        "session_id": self.global_domain.session_id,
                        "workspace_id": self.global_domain.workspace_id,
                        "status": "success",
                        "duration_s": (end_ts - request_start_ts).total_seconds(),
                        "scene_result": {
                            "flow_scenarios": [
                                scenario.scenario_id for scenario in scenarios
                            ]
                        },
                        "agents_called": [
                            result.name
                            for result in dispatch_results
                            if hasattr(result, "name")
                        ],
                        "token_usage_total": meta.get("token_usage"),
                        "error": None,
                    },
                )
            )

            yield FlowDomainStreamEvent(
                event="done",
                data={
                    "content": final_content,
                    "attachment_results": serialize_attachment_results(
                        self.attachment_collector.list_items()
                    ),
                    "tool_extra_results": serialize_tool_extra_results(
                        self.tool_extra_result_collector.list_items()
                    ),
                    "meta": meta,
                    "request_id": self.request_id,
                    "state_id": self.state_id,
                    "finished": True,
                },
            )
        except Exception as exc:
            logger.exception("Flow domain pipeline failed")
            async for event in self._fallback_to_global_domain(
                request=request,
                reason=f"flow_execution_error:{type(exc).__name__}",
                message=f"心流模式执行失败: {type(exc).__name__}",
            ):
                yield event

    async def consume_event_stream(
        self,
        request: FlowChatRequest,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        attachment_results = None
        tool_extra_results = None
        meta: dict[str, Any] = {}
        error_message: str | None = None

        async for event in self.pipeline_stream(request):
            if event.event == "meta":
                data = event.data
                if data.get("phase") == "agent_action":
                    continue
                meta.update(data)
                continue

            if event.event == "content_delta":
                content = event.data.get("content")
                if content:
                    content_parts.append(str(content))
                continue

            if event.event == "done":
                data = event.data
                attachment_results = normalize_attachment_results(
                    data.get("attachment_results")
                )
                tool_extra_results = normalize_tool_extra_results(
                    data.get("tool_extra_results")
                )
                done_meta = data.get("meta")
                if isinstance(done_meta, dict):
                    meta.update(done_meta)
                return {
                    "content": str(data.get("content") or ""),
                    "attachment_results": attachment_results,
                    "tool_extra_results": tool_extra_results,
                    "meta": meta,
                }

            if event.event == "error":
                error_message = str(event.data.get("error") or "flow stream failed")

        if error_message:
            return {
                "content": "",
                "attachment_results": attachment_results,
                "tool_extra_results": tool_extra_results,
                "meta": {**meta, "error": error_message, "finished": True},
            }

        return {
            "content": "".join(content_parts),
            "attachment_results": attachment_results,
            "tool_extra_results": tool_extra_results,
            "meta": meta,
        }
