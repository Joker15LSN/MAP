"""P2-9.2 acceptance tests: both engines satisfy the RuntimeAgent protocol.

Regression for the review finding that AgentRuntime annotated everything as
``ToolCallAgent`` while it may actually hand out ``AgentScopeSceneAgent``.
"""

from __future__ import annotations

from map_core.service.agent.tool_call_agent import ToolCallAgent, ToolSet
from map_core.service.agent_runtime import RuntimeAgent
from map_core.service.agentscope2.agent import AgentScopeSceneAgent


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"


class FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeLLMConfig()


def test_legacy_agent_satisfies_protocol() -> None:
    agent = ToolCallAgent(
        llm=FakeLLM(),
        name="LegacyAgent",
        system_prompt="test",
        toolset=ToolSet([], include_terminate=False),
    )
    assert isinstance(agent, RuntimeAgent)


def test_agentscope_agent_satisfies_protocol() -> None:
    agent = AgentScopeSceneAgent(
        llm=FakeLLM(),
        name="ScopeAgent",
        system_prompt="test",
        additional_user_prompt="",
        tools=[],
        max_steps=3,
        force_tool_call=False,
        scene_post_summary=None,
    )
    assert isinstance(agent, RuntimeAgent)
