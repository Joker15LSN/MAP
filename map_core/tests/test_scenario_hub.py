from map_core.schema.flow_domain_schema import (
    ScenarioPolicyConfigSchema,
    StepVerdictSchema,
)
from map_core.service.scenario_hub import ScenarioHub


def test_scenario_resolve_and_allowed_filter() -> None:
    hub = ScenarioHub()
    query = "订单可以确认收入吗？"

    matched = hub.resolve(
        query=query,
        scenario_policy=ScenarioPolicyConfigSchema(),
    )
    assert matched
    assert matched[0].scenario_id == "order_revenue_confirmation"

    filtered = hub.resolve(
        query=query,
        scenario_policy=ScenarioPolicyConfigSchema(
            allowed_scenarios=["cross_domain_serial_analysis"],
        ),
    )
    assert filtered == []


def test_scenario_build_graph_and_repair_suggestion() -> None:
    hub = ScenarioHub()
    scenario = hub.get_by_id("order_revenue_confirmation")
    assert scenario is not None

    graph = hub.build_graph(scenarios=[scenario], max_node_budget=8)
    assert graph.nodes
    assert graph.nodes[0].agent_code == "Supply_Chain"
    assert graph.edges

    verdict = hub.suggest_repair(
        node=graph.nodes[0],
        verdict=StepVerdictSchema(
            node_id=graph.nodes[0].node_id,
            verdict="uncertain",
            missing_evidence=["delivery_record"],
        ),
    )
    assert verdict
    assert verdict[0].target_agent == graph.nodes[0].agent_code
