from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..schema.flow_domain_schema import ScenarioPackSchema, SkillDescriptorSchema


@dataclass
class SkillMountPlan:
    allowed_tools: list[str]
    tool_context_overlay: dict[str, Any]
    authorized_skills: list[dict[str, Any]]
    denied_skills: list[dict[str, Any]]


class SkillHub:
    """Local static SkillHub for v1 flow mode."""

    def __init__(self) -> None:
        self._skills = self.default_skills()

    @staticmethod
    def default_skills() -> list[SkillDescriptorSchema]:
        return [
            SkillDescriptorSchema(
                skill_id="ops.ask_database.v1",
                name="ask_database",
                display_name="经营问表",
                tool_name="ask_database_agent",
                mount_agents=["Operations", "Supply_Chain", "Procurement"],
                required_scopes=["skill:ask_database:execute"],
                audit_tags=["builtin", "data"],
            ),
            SkillDescriptorSchema(
                skill_id="ops.wenshu.v1",
                name="wenshu",
                display_name="问数",
                tool_name="wenshu_agent",
                mount_agents=["Operations", "Supply_Chain", "Procurement"],
                required_scopes=["skill:wenshu:execute"],
                audit_tags=["builtin", "metrics"],
            ),
            SkillDescriptorSchema(
                skill_id="ops.web_search.v1",
                name="web_search",
                display_name="互联网检索",
                tool_name="web_search_agent",
                mount_agents=["Operations", "General_Assistant", "Procurement"],
                required_scopes=["skill:web_search:execute"],
                audit_tags=["builtin", "external"],
            ),
            SkillDescriptorSchema(
                skill_id="ops.efficiency_pi.v1",
                name="efficiency_pi",
                display_name="效率派",
                tool_name="efficiency_pi_agent",
                mount_agents=["General_Assistant"],
                required_scopes=["skill:efficiency_pi:execute"],
                audit_tags=["builtin", "ops"],
            ),
        ]

    def load_external_skills(
        self,
        skills: list[SkillDescriptorSchema] | None,
    ) -> None:
        self._skills = list(skills) if skills else self.default_skills()

    def list_by_agent(
        self,
        *,
        agent_code: str,
        scenarios: list[ScenarioPackSchema],
        base_tool_context: dict[str, Any] | None,
        user_id: str,
        tenant: str,
    ) -> SkillMountPlan:
        scenario_scopes = {
            scope for scenario in scenarios for scope in scenario.auth_scopes
        }
        scenario_ids = {scenario.scenario_id for scenario in scenarios}
        normalized_user_id = (user_id or "").strip() or "missing"
        normalized_tenant = (tenant or "").strip() or "missing"
        action = "execute"

        allowed_tools: list[str] = []
        authorized_skills: list[dict[str, Any]] = []
        denied_skills: list[dict[str, Any]] = []

        def _is_any_match(allowed_values: list[str], current: str) -> bool:
            normalized = {item.strip() for item in allowed_values if item.strip()}
            return "*" in normalized or current in normalized

        for skill in self._skills:
            if skill.status != "active":
                denied_skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "tool_name": skill.tool_name,
                        "reason": "inactive",
                    }
                )
                continue
            if agent_code not in skill.mount_agents:
                denied_skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "tool_name": skill.tool_name,
                        "reason": "agent_not_mounted",
                    }
                )
                continue

            if not _is_any_match(skill.allowed_users, normalized_user_id):
                denied_skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "tool_name": skill.tool_name,
                        "reason": "user_denied",
                    }
                )
                continue
            if not _is_any_match(skill.allowed_tenants, normalized_tenant):
                denied_skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "tool_name": skill.tool_name,
                        "reason": "tenant_denied",
                    }
                )
                continue
            if skill.allowed_scenarios and not scenario_ids.intersection(
                set(skill.allowed_scenarios)
            ):
                denied_skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "tool_name": skill.tool_name,
                        "reason": "scenario_denied",
                    }
                )
                continue
            if skill.allowed_actions and action not in {
                item.strip() for item in skill.allowed_actions if item.strip()
            }:
                denied_skills.append(
                    {
                        "skill_id": skill.skill_id,
                        "tool_name": skill.tool_name,
                        "reason": "action_denied",
                    }
                )
                continue
            if skill.required_scopes and scenario_scopes:
                if not scenario_scopes.intersection(skill.required_scopes) and not any(
                    scope.startswith("scenario:") for scope in scenario_scopes
                ):
                    denied_skills.append(
                        {
                            "skill_id": skill.skill_id,
                            "tool_name": skill.tool_name,
                            "reason": "scope_denied",
                        }
                    )
                    continue
            if skill.tool_name not in allowed_tools:
                allowed_tools.append(skill.tool_name)
            authorized_skills.append(
                {
                    "skill_id": skill.skill_id,
                    "tool_name": skill.tool_name,
                    "authorized_by": "skill_hub_policy",
                    "matched_agent": agent_code,
                    "matched_scenarios": sorted(scenario_ids),
                    "audit_tags": list(skill.audit_tags),
                }
            )

        overlay = self._build_overlay(
            agent_code=agent_code,
            base_tool_context=base_tool_context,
            allowed_tools=allowed_tools,
        )
        return SkillMountPlan(
            allowed_tools=allowed_tools,
            tool_context_overlay=overlay,
            authorized_skills=authorized_skills,
            denied_skills=denied_skills,
        )

    @staticmethod
    def _build_overlay(
        *,
        agent_code: str,
        base_tool_context: dict[str, Any] | None,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        base_context = dict(base_tool_context or {})
        agent_context = base_context.get(agent_code)
        if not isinstance(agent_context, dict):
            agent_context = {}

        patched_agent_context = dict(agent_context)
        for tool_name in allowed_tools:
            tool_context = patched_agent_context.get(tool_name)
            if not isinstance(tool_context, dict):
                tool_context = {}
            patched_agent_context[tool_name] = {
                **tool_context,
                "skill_hub_mounted": True,
            }

        merged = dict(base_context)
        merged[agent_code] = patched_agent_context
        return merged
