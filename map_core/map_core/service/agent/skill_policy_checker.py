from __future__ import annotations

from typing import Any

from .base import AgentRequest


class SkillPolicyChecker:
    """Runtime authorization for tool execution in flow mode."""

    RUNTIME_SWITCH_KEY = "skill_policy_runtime_auth_check"
    ALLOWED_TOOLS_KEY = "skill_policy_allowed_tools"
    ALLOWED_TOOLS_BY_AGENT_KEY = "skill_policy_allowed_tools_by_agent"
    AUTHORIZATION_MAP_KEY = "skill_policy_authorization_map"
    CONTEXT_KEY = "skill_policy_context"

    @classmethod
    def is_allowed(
        cls,
        *,
        request: AgentRequest,
        agent_code: str,
        tool_name: str,
    ) -> bool:
        verdict = cls.evaluate(
            request=request,
            agent_code=agent_code,
            tool_name=tool_name,
            action="execute",
        )
        return bool(verdict.get("allowed", True))

    @classmethod
    def evaluate(
        cls,
        *,
        request: AgentRequest,
        agent_code: str,
        tool_name: str,
        action: str,
    ) -> dict[str, Any]:
        extra = request.extra or {}
        if not bool(extra.get(cls.RUNTIME_SWITCH_KEY, False)):
            return {"allowed": True, "reason": "runtime_switch_off"}

        context = extra.get(cls.CONTEXT_KEY)
        if not isinstance(context, dict):
            context = {}
        user_id = str(context.get("user_id", "missing"))
        tenant = str(context.get("tenant", "missing"))
        scenario_ids = context.get("scenario_ids")
        if not isinstance(scenario_ids, list):
            scenario_ids = []

        auth_map = extra.get(cls.AUTHORIZATION_MAP_KEY)
        if isinstance(auth_map, dict):
            item = auth_map.get(tool_name)
            if isinstance(item, dict):
                allowed_agent = item.get("agent_code")
                if allowed_agent and str(allowed_agent) != agent_code:
                    return {
                        "allowed": False,
                        "reason": "agent_code_mismatch",
                        "agent_code": agent_code,
                        "tool": tool_name,
                        "action": action,
                        "user_id": user_id,
                        "tenant": tenant,
                        "scenario_ids": scenario_ids,
                    }
                if item.get("allowed") is False:
                    return {
                        "allowed": False,
                        "reason": str(item.get("reason") or "policy_denied"),
                        "agent_code": agent_code,
                        "tool": tool_name,
                        "action": action,
                        "user_id": user_id,
                        "tenant": tenant,
                        "scenario_ids": scenario_ids,
                    }

        by_agent = extra.get(cls.ALLOWED_TOOLS_BY_AGENT_KEY)
        if isinstance(by_agent, dict):
            scoped = by_agent.get(agent_code)
            if isinstance(scoped, list):
                allowed = tool_name in {str(item) for item in scoped}
                return {
                    "allowed": allowed,
                    "reason": "allowed_tools_by_agent"
                    if allowed
                    else "tool_not_in_agent_scope",
                    "agent_code": agent_code,
                    "tool": tool_name,
                    "action": action,
                    "user_id": user_id,
                    "tenant": tenant,
                    "scenario_ids": scenario_ids,
                }

        allowed = extra.get(cls.ALLOWED_TOOLS_KEY)
        if isinstance(allowed, list):
            is_allowed = tool_name in {str(item) for item in allowed}
            return {
                "allowed": is_allowed,
                "reason": "allowed_tools"
                if is_allowed
                else "tool_not_in_allowed_tools",
                "agent_code": agent_code,
                "tool": tool_name,
                "action": action,
                "user_id": user_id,
                "tenant": tenant,
                "scenario_ids": scenario_ids,
            }

        return {
            "allowed": True,
            "reason": "no_allow_list_configured",
            "agent_code": agent_code,
            "tool": tool_name,
            "action": action,
            "user_id": user_id,
            "tenant": tenant,
            "scenario_ids": scenario_ids,
        }

    @staticmethod
    def denied_result(
        *,
        tool_name: str,
        agent_code: str,
        reason: str = "tool_forbidden",
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "error": "tool denied by skill policy",
            "code": "tool_forbidden",
            "tool": tool_name,
            "agent_code": agent_code,
            "reason": reason,
            "auth_context": auth_context or {},
        }
