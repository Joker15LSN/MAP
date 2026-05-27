from __future__ import annotations

from typing import Any

from ..schema.flow_domain_schema import (
    ScenarioPackSchema,
    ScenarioPolicyConfigSchema,
)
from .scenario_hub import ScenarioHub


class ScenarioResolver:
    """Resolve executable scenarios from query + policy + runtime tool context."""

    def resolve(
        self,
        *,
        query: str,
        scenario_policy: ScenarioPolicyConfigSchema,
        tool_context: dict[str, Any] | None,
        scenario_hub: ScenarioHub,
    ) -> list[ScenarioPackSchema]:
        matched = scenario_hub.resolve(
            query=query,
            scenario_policy=scenario_policy,
        )
        if not matched:
            return []

        context_matched = (
            (tool_context or {}).get("scenario", {}).get("matched_scenarios")
            if isinstance((tool_context or {}).get("scenario"), dict)
            else None
        )
        if not isinstance(context_matched, list) or not context_matched:
            return matched

        allowed_from_context = {str(item).strip() for item in context_matched if str(item).strip()}
        filtered = [
            item
            for item in matched
            if item.scenario_id in allowed_from_context
        ]
        return filtered or matched
