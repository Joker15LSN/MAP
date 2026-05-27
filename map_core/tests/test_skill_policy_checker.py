from map_core.service.agent.base import AgentRequest
from map_core.service.agent.skill_policy_checker import SkillPolicyChecker


def test_skill_policy_checker_runtime_deny_and_allow() -> None:
    request = AgentRequest(
        query="q",
        staff_code="s",
        extra={
            "skill_policy_runtime_auth_check": True,
            "skill_policy_allowed_tools_by_agent": {
                "Operations": ["wenshu_agent"],
            },
        },
    )

    assert SkillPolicyChecker.is_allowed(
        request=request,
        agent_code="Operations",
        tool_name="wenshu_agent",
    )
    assert not SkillPolicyChecker.is_allowed(
        request=request,
        agent_code="Operations",
        tool_name="ask_database_agent",
    )


def test_skill_policy_checker_default_allow_when_switch_off() -> None:
    request = AgentRequest(
        query="q",
        staff_code="s",
        extra={
            "skill_policy_runtime_auth_check": False,
            "skill_policy_allowed_tools": [],
        },
    )
    assert SkillPolicyChecker.is_allowed(
        request=request,
        agent_code="Operations",
        tool_name="any_tool",
    )


def test_skill_policy_checker_authorization_map_denies_tool() -> None:
    request = AgentRequest(
        query="q",
        staff_code="s",
        extra={
            "skill_policy_runtime_auth_check": True,
            "skill_policy_authorization_map": {
                "ask_database_agent": {
                    "allowed": False,
                    "reason": "tenant_denied",
                    "agent_code": "Operations",
                }
            },
            "skill_policy_context": {
                "user_id": "u1",
                "tenant": "t1",
                "scenario_ids": ["s1"],
            },
        },
    )

    verdict = SkillPolicyChecker.evaluate(
        request=request,
        agent_code="Operations",
        tool_name="ask_database_agent",
        action="execute",
    )
    assert verdict["allowed"] is False
    assert verdict["reason"] == "tenant_denied"
