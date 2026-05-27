from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..schema.flow_domain_schema import ScenarioPackSchema


def _now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class AgentCaseMiner:
    """Extract lightweight reusable AgentCase payload from flow runtime trace."""

    @staticmethod
    def build_agent_case(
        *,
        query: str,
        scenarios: list[ScenarioPackSchema],
        graph_trace: dict[str, Any],
        final_content: str,
    ) -> dict[str, Any]:
        return {
            "case_id": f"case_{int(datetime.now().timestamp())}",
            "created_at": _now_iso(),
            "query": query,
            "scenario_ids": [item.scenario_id for item in scenarios],
            "final_summary": final_content[:5000],
            "graph_trace": graph_trace,
            "status": "candidate",
        }

    @staticmethod
    def build_repair_policy_candidates(
        *,
        scenarios: list[ScenarioPackSchema],
        graph_trace: dict[str, Any],
    ) -> list[dict[str, Any]]:
        verdicts = graph_trace.get("step_verdicts") if isinstance(graph_trace, dict) else None
        if not isinstance(verdicts, list):
            return []
        failed = [
            item for item in verdicts
            if isinstance(item, dict) and item.get("verdict") in {"fail", "uncertain"}
        ]
        if not failed:
            return []
        return [
            {
                "candidate_id": f"repair_{idx + 1}",
                "scenario_ids": [item.scenario_id for item in scenarios],
                "source_node_id": row.get("node_id"),
                "missing_evidence": row.get("missing_evidence", []),
                "issues": row.get("issues", []),
                "status": "draft",
            }
            for idx, row in enumerate(failed[:5])
        ]
