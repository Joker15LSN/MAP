import asyncio

from map_core.schema.flow_domain_schema import FlowConfigSchema, ScenarioPackSchema, SkillDescriptorSchema
from map_core.service.flow_config_provider import FlowConfigProvider, FlowConfigSnapshot


def test_flow_config_provider_static_snapshot(monkeypatch) -> None:
    provider = FlowConfigProvider()
    monkeypatch.setattr(provider, "fetch_enabled", False)

    snapshot = asyncio.run(provider.get_snapshot())
    assert snapshot.source == "static"
    assert isinstance(snapshot.flow_policy, FlowConfigSchema)
    assert isinstance(snapshot.scenario_packs, list)
    assert isinstance(snapshot.flow_skill_descriptors, list)


def test_flow_config_provider_cache_fallback(monkeypatch) -> None:
    provider = FlowConfigProvider()
    cached = FlowConfigSnapshot(
        source="cache",
        flow_policy=FlowConfigSchema(max_node_budget=21),
        scenario_packs=[
            ScenarioPackSchema(
                scenario_id="s1",
                display_name="S1",
                domain="d1",
            )
        ],
        flow_skill_descriptors=[
            SkillDescriptorSchema(
                skill_id="k1",
                name="k1",
                display_name="k1",
                tool_name="ask_database_agent",
            )
        ],
        stale=False,
    )
    provider._cache = cached  # noqa: SLF001
    monkeypatch.setattr(provider, "_is_cache_fresh", lambda: False)
    monkeypatch.setattr(provider, "_try_fetch_remote", lambda: asyncio.sleep(0, result=None))

    snapshot = asyncio.run(provider.get_snapshot())
    assert snapshot.source == "cache"
    assert snapshot.stale is True
    assert snapshot.flow_policy.max_node_budget == 21
