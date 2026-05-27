from __future__ import annotations

from ..schema.flow_domain_schema import BusinessExecutionGraphSchema, ScenarioPackSchema
from .scenario_hub import ScenarioHub


class HyperedgePlanner:
    """Build execution graph from resolved scenarios and scenario templates."""

    def build_execution_graph(
        self,
        *,
        scenarios: list[ScenarioPackSchema],
        max_node_budget: int,
        scenario_hub: ScenarioHub,
    ) -> BusinessExecutionGraphSchema:
        return scenario_hub.build_graph(
            scenarios=scenarios,
            max_node_budget=max_node_budget,
        )
