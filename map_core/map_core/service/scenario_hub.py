from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from ..schema.flow_domain_schema import (
    BusinessExecutionGraphSchema,
    GraphEdgeSchema,
    GraphNodeSchema,
    RepairCandidateSchema,
    ScenarioPackSchema,
    ScenarioPolicyConfigSchema,
    StepVerdictSchema,
)


@dataclass(frozen=True)
class _ScenarioGraphTemplate:
    hyperedge_id: str
    evidence_contract: list[str]


class ScenarioHub:
    """Local static ScenarioHub for v1 flow mode."""

    def __init__(self) -> None:
        self._scenario_packs = self.default_scenario_packs()
        self._scenario_templates = self.default_templates()

    @staticmethod
    def default_scenario_packs() -> dict[str, ScenarioPackSchema]:
        return {
            "order_revenue_confirmation": ScenarioPackSchema(
                scenario_id="order_revenue_confirmation",
                display_name="订单确认收入",
                domain="finance_supply_chain",
                description="跨财务与供应链确认订单收入可确认性",
                trigger_intents=[
                    "订单确认收入",
                    "收入确认",
                    "发货后能否确认收入",
                    "订单可以确认收入吗",
                ],
                required_agents=["Supply_Chain", "Procurement", "Operations"],
                auth_scopes=["scenario:order_revenue_confirmation:read"],
            ),
            "cross_domain_serial_analysis": ScenarioPackSchema(
                scenario_id="cross_domain_serial_analysis",
                display_name="跨域串行分析",
                domain="general_enterprise",
                description="跨业务域的串行查询与证据拼接",
                trigger_intents=["跨业务域", "串行", "心流", "分步骤"],
                required_agents=["Operations", "General_Assistant"],
                auth_scopes=["scenario:cross_domain_serial_analysis:read"],
            ),
        }

    @staticmethod
    def default_templates() -> dict[str, _ScenarioGraphTemplate]:
        return {
            "order_revenue_confirmation": _ScenarioGraphTemplate(
                hyperedge_id="revenue_confirmable_after_delivery",
                evidence_contract=[
                    "delivery_record",
                    "contract_terms",
                    "invoice_status",
                ],
            ),
            "cross_domain_serial_analysis": _ScenarioGraphTemplate(
                hyperedge_id="cross_domain_chain_reasoning",
                evidence_contract=["domain_fact", "cross_validation"],
            ),
        }

    def load_external_scenarios(
        self,
        scenarios: list[ScenarioPackSchema] | None,
    ) -> None:
        if not scenarios:
            self._scenario_packs = self.default_scenario_packs()
            self._scenario_templates = self.default_templates()
            return

        loaded: dict[str, ScenarioPackSchema] = {}
        for item in scenarios:
            loaded[item.scenario_id] = item
        self._scenario_packs = loaded

        defaults = self.default_templates()
        self._scenario_templates = {
            scenario_id: defaults.get(
                scenario_id,
                _ScenarioGraphTemplate(
                    hyperedge_id=f"{scenario_id}_default_hyperedge",
                    evidence_contract=[],
                ),
            )
            for scenario_id in loaded
        }

    def resolve(
        self,
        *,
        query: str,
        scenario_policy: ScenarioPolicyConfigSchema,
    ) -> list[ScenarioPackSchema]:
        if not scenario_policy.enabled:
            return []

        allowed = set(scenario_policy.allowed_scenarios or [])
        active_candidates = [
            pack
            for pack in self._scenario_packs.values()
            if pack.status == "active"
            and (not allowed or pack.scenario_id in allowed)
        ]

        if scenario_policy.mode == "manual":
            return active_candidates

        lowered_query = query.lower()
        matched: list[ScenarioPackSchema] = []
        for pack in active_candidates:
            if any(term.lower() in lowered_query for term in pack.trigger_intents):
                matched.append(pack)
        return matched

    def build_graph(
        self,
        *,
        scenarios: list[ScenarioPackSchema],
        max_node_budget: int,
    ) -> BusinessExecutionGraphSchema:
        nodes: list[GraphNodeSchema] = []
        edges: list[GraphEdgeSchema] = []
        scenario_ids = [scenario.scenario_id for scenario in scenarios]
        activated_hyperedges: list[str] = []

        for scenario in scenarios:
            template = self._scenario_templates.get(scenario.scenario_id)
            if template is None:
                continue
            activated_hyperedges.append(template.hyperedge_id)
            previous_node_id: str | None = None
            for index, agent_code in enumerate(scenario.required_agents):
                if len(nodes) >= max_node_budget:
                    break
                node_id = f"{scenario.scenario_id}_{index + 1}_{agent_code}"
                node = GraphNodeSchema(
                    node_id=node_id,
                    scenario_id=scenario.scenario_id,
                    agent_code=agent_code,
                    goal=f"完成 {scenario.display_name} 的第 {index + 1} 步证据获取",
                    evidence_contract=list(template.evidence_contract),
                    allowed_capabilities=[],
                    status="pending",
                    depends_on=[previous_node_id] if previous_node_id else [],
                )
                nodes.append(node)
                if previous_node_id:
                    edges.append(
                        GraphEdgeSchema(
                            **{
                                "from": previous_node_id,
                                "to": node_id,
                            }
                        )
                    )
                previous_node_id = node_id

        return BusinessExecutionGraphSchema(
            graph_id=f"beg_{uuid4().hex}",
            scenario_ids=scenario_ids,
            activated_hyperedges=activated_hyperedges,
            nodes=nodes,
            edges=edges,
            repair_candidates=[],
        )

    def suggest_repair(
        self,
        *,
        node: GraphNodeSchema,
        verdict: StepVerdictSchema,
    ) -> list[RepairCandidateSchema]:
        if verdict.verdict == "pass":
            return []

        if verdict.missing_evidence:
            reason = "需要补充缺失证据: " + "、".join(verdict.missing_evidence[:3])
        elif verdict.issues:
            reason = "需要处理执行异常: " + "；".join(verdict.issues[:2])
        else:
            reason = "当前证据不足，需要补证"

        return [
            RepairCandidateSchema(
                action=f"repair_{node.node_id}",
                target_agent=node.agent_code,
                reason=reason,
            )
        ]

    def get_by_id(self, scenario_id: str) -> ScenarioPackSchema | None:
        return self._scenario_packs.get(scenario_id)
