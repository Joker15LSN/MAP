from __future__ import annotations

from dataclasses import dataclass, field

from ..schema.flow_domain_schema import (
    BusinessExecutionGraphSchema,
    GraphNodeSchema,
    NodeExecutionResultSchema,
    StepVerdictSchema,
)


@dataclass
class GraphRuntimeState:
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


class BusinessExecutionGraphStore:
    """In-memory graph runtime store for one request lifecycle."""

    @staticmethod
    def create(graph: BusinessExecutionGraphSchema) -> GraphRuntimeState:
        return GraphRuntimeState(graph=graph)

    @staticmethod
    def append_repair_node(
        state: GraphRuntimeState,
        *,
        repair_node: GraphNodeSchema,
    ) -> None:
        state.graph.nodes.append(repair_node)
