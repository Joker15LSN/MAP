from map_core.schema.flow_domain_schema import ScenarioPackSchema
from map_core.service.skill_hub import SkillHub


def test_skill_hub_agent_mount_plan() -> None:
    hub = SkillHub()
    scenario = ScenarioPackSchema(
        scenario_id="order_revenue_confirmation",
        display_name="订单确认收入",
        domain="finance_supply_chain",
        auth_scopes=["scenario:order_revenue_confirmation:read"],
    )

    plan = hub.list_by_agent(
        agent_code="Operations",
        scenarios=[scenario],
        base_tool_context={"Operations": {"ask_database_agent": {"user_id": 1}}},
        user_id="u1",
        tenant="t1",
    )

    assert "ask_database_agent" in plan.allowed_tools
    assert "Operations" in plan.tool_context_overlay
    assert plan.tool_context_overlay["Operations"]["ask_database_agent"]["skill_hub_mounted"] is True
    assert isinstance(plan.authorized_skills, list)
    assert isinstance(plan.denied_skills, list)
