"""P2-4 acceptance tests: dual engine switch (request > AdminState > env)."""

from __future__ import annotations

import asyncio

from map_core.config.config_schema import LLMConfig
from map_core.service.agent.agent_mapping import SceneAgentConfig
from map_core.service.agent.tool_call_agent import ToolCallAgent
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agent_dispatcher import AgentDispatchConfig, AgentDispatcher
from map_core.service.agent_runtime import (
    AgentExecutionSpec,
    AgentRuntime,
    resolve_agent_engine,
)
from map_core.service.agentscope2.agent import AgentScopeSceneAgent
from map_core.utils.llm_engine import LLMEngine


def _runtime() -> AgentRuntime:
    llm = LLMEngine(
        config=LLMConfig(
            base_url="http://localhost:9/v1",
            api_key="k",
            model="m",
        )
    )
    tool = Tool(name="search", description="d", parameters={})
    return AgentRuntime(llm=llm, tool_registry={"search": tool})


def _spec(**overrides) -> AgentExecutionSpec:
    base = {
        "name": "TestAgent",
        "system_prompt": "be helpful",
        "tool_names": ["search"],
        "max_steps": 2,
    }
    base.update(overrides)
    return AgentExecutionSpec(**base)


def test_resolve_engine_priority(monkeypatch) -> None:
    # request-level wins over env
    monkeypatch.setenv("MAP_AGENT_ENGINE", "legacy")
    assert resolve_agent_engine("agentscope") == "agentscope"

    # env wins when request-level is absent
    assert resolve_agent_engine(None) == "legacy"
    monkeypatch.setenv("MAP_AGENT_ENGINE", "agentscope")
    assert resolve_agent_engine(None) == "agentscope"

    # invalid values fall back to legacy
    monkeypatch.setenv("MAP_AGENT_ENGINE", "bogus")
    assert resolve_agent_engine(None) == "legacy"
    monkeypatch.delenv("MAP_AGENT_ENGINE")
    assert resolve_agent_engine(None) == "legacy"


def test_build_agent_engine_branches() -> None:
    runtime = _runtime()

    legacy_agent = runtime.build_agent(_spec())
    assert isinstance(legacy_agent, ToolCallAgent)

    new_agent = runtime.build_agent(_spec(engine="agentscope"))
    assert isinstance(new_agent, AgentScopeSceneAgent)

    # both engines expose the same runtime contract
    for agent in (legacy_agent, new_agent):
        assert agent.name == "TestAgent"
        assert hasattr(agent, "execute")
        assert hasattr(agent, "set_action_handler")
        assert isinstance(agent.token_usage, dict)


def test_build_agent_env_switch(monkeypatch) -> None:
    runtime = _runtime()
    monkeypatch.setenv("MAP_AGENT_ENGINE", "agentscope")
    agent = runtime.build_agent(_spec())
    assert isinstance(agent, AgentScopeSceneAgent)


def test_dispatcher_propagates_dispatch_config_engine() -> None:
    llm = LLMEngine(
        config=LLMConfig(
            base_url="http://localhost:9/v1",
            api_key="k",
            model="m",
        )
    )
    tool = Tool(name="search", description="d", parameters={})
    dispatcher = AgentDispatcher(
        llm=llm,
        tool_registry={"search": tool},
        scene_agent_configs={
            "TestAgent": SceneAgentConfig(
                prompt="be helpful",
                tool_names=["search"],
                max_steps=2,
            )
        },
    )

    from map_core.service.agent.base import AgentRequest

    request = AgentRequest(query="hi", staff_code="t")

    legacy_agents = dispatcher.available_agents(request, AgentDispatchConfig())
    assert legacy_agents and isinstance(legacy_agents[0], ToolCallAgent)

    new_agents = dispatcher.available_agents(
        request,
        AgentDispatchConfig(engine="agentscope"),
    )
    assert new_agents and isinstance(new_agents[0], AgentScopeSceneAgent)


def test_two_engines_same_request_same_contract() -> None:
    """Same request executed by both engines yields the same result contract."""
    from map_core.schema.agent_schema import Function, ToolCall
    from map_core.service.agent.base import AgentRequest
    from map_core.utils.llm_engine import ToolCallResponse

    class ScriptedLLM:
        def __init__(self) -> None:
            self.config = LLMConfig(
                base_url="http://localhost:9/v1",
                api_key="k",
                model="m",
            )

        async def ask_tool(self, messages, tools=None, tool_choice=None, **kwargs):
            return ToolCallResponse(
                content="最终答案",
                tool_calls=[
                    ToolCall(
                        id="call_x",
                        function=Function(name="terminate", arguments="{}"),
                    )
                ],
                usage={"prompt_tokens": 1, "completion_tokens": 2},
                finish_reason="tool_calls",
            )

    tool = Tool(
        name="search",
        description="d",
        parameters={},
        handler=lambda args, request, parid: "tool output",
    )

    results = []
    for engine in ("legacy", "agentscope"):
        runtime = AgentRuntime(
            llm=ScriptedLLM(),
            tool_registry={"search": tool},
        )
        agent = runtime.build_agent(_spec(engine=engine, force_tool_call=False))
        request = AgentRequest(query="请回答", staff_code="t")
        result = asyncio.run(
            runtime.run_agent(agent, request, action_handler=None)
        )
        results.append(result)

    legacy_result, new_result = results
    # identical contract fields
    for field in ("name", "success"):
        assert getattr(legacy_result, field) == getattr(new_result, field), field
    assert legacy_result.content == new_result.content == "最终答案"
    assert new_result.meta_data.get("agent_code") == "TestAgent"
