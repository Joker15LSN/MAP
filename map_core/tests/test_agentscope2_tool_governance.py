"""P2-2 acceptance tests: MapToolAdapter routes through MAP tool governance.

Verifies that in the AgentScope engine a tool call denied by
SkillPolicyChecker is rejected before execution and the denial is recorded
as runtime events (action stream + state store events).
"""

from __future__ import annotations

import asyncio
from typing import Any

from map_core.service.agent.base import AgentRequest
from map_core.service.agent.skill_policy_checker import SkillPolicyChecker
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agentscope2.agent import AgentScopeSceneAgent
from map_core.service.agentscope2.tool import MapToolAdapter
from map_core.service.execution_event import set_run_context
from tests.run_context_utils import make_run_context_sink


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"


class FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeLLMConfig()

    async def invoke(self, req: Any) -> Any:
        raise AssertionError("LLM should not be called in governance tests")


class FakeStateStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def record_event(self, *, state_id, event_type, payload):
        self.events.append((event_type, payload))


def _build_agent(action_events: list[Any], store: FakeStateStore):
    handler_called: list[bool] = []

    def forbidden_handler(args, request, parid):
        handler_called.append(True)
        return "should never run"

    tool = Tool(
        name="secret_tool",
        description="restricted tool",
        parameters={"type": "object", "properties": {}},
        handler=forbidden_handler,
    )
    agent = AgentScopeSceneAgent(
        llm=FakeLLM(),
        name="TestAgent",
        system_prompt="you are a test agent",
        additional_user_prompt="",
        tools=[tool],
        max_steps=3,
        force_tool_call=False,
        scene_post_summary=None,
    )
    agent.set_action_handler(lambda event: action_events.append(event))
    del store  # typed events are captured via the run-context sink in each test
    return agent, tool, handler_called


def test_denied_tool_not_executed_and_events_recorded() -> None:
    action_events: list[Any] = []
    store = FakeStateStore()
    agent, tool, handler_called = _build_agent(action_events, store)

    request = AgentRequest(
        query="run the secret tool",
        staff_code="tester",
        extra={
            SkillPolicyChecker.RUNTIME_SWITCH_KEY: True,
            SkillPolicyChecker.ALLOWED_TOOLS_KEY: ["another_tool"],
        },
    )
    adapter = MapToolAdapter(
        tool=tool,
        function_schema=tool.to_openai_tool(request, owner_agent_name=agent.name),
        owner=agent,
        request=request,
    )

    run_context, sink = make_run_context_sink()

    async def run():
        with set_run_context(run_id=run_context.run_id):
            chunk = await adapter.call()
            # allow emitter worker tasks to settle
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return chunk

    chunk = asyncio.run(run())

    assert not handler_called, "denied tool handler must not execute"
    output_text = chunk.content[0].text
    assert "tool_forbidden" in output_text

    # SSE action stream carries the denial with the policy verdict
    tool_result_actions = [
        event for event in action_events if event.action == "tool_result"
    ]
    assert tool_result_actions
    payload = tool_result_actions[0].payload
    assert payload["tool_name"] == "secret_tool"
    assert payload["policy"]["allowed"] is False
    assert payload["policy"]["reason"] == "tool_not_in_allowed_tools"

    # typed emitter receives the failed tool invocation runtime event
    recorded_types = [event.type for event in sink.events]
    assert "tool.invocation_failed" in recorded_types
    failed_tool = next(
        event for event in sink.events if event.type == "tool.invocation_failed"
    )
    assert failed_tool.data["tool"] == "secret_tool"


def test_allowed_tool_executes_through_adapter() -> None:
    action_events: list[Any] = []
    store = FakeStateStore()
    agent, tool, handler_called = _build_agent(action_events, store)

    request = AgentRequest(
        query="run the secret tool",
        staff_code="tester",
        extra={
            SkillPolicyChecker.RUNTIME_SWITCH_KEY: True,
            SkillPolicyChecker.ALLOWED_TOOLS_KEY: ["secret_tool"],
        },
    )
    adapter = MapToolAdapter(
        tool=tool,
        function_schema=tool.to_openai_tool(request, owner_agent_name=agent.name),
        owner=agent,
        request=request,
    )

    run_context, sink = make_run_context_sink()

    async def run():
        with set_run_context(run_id=run_context.run_id):
            chunk = await adapter.call()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return chunk

    chunk = asyncio.run(run())

    assert handler_called, "allowed tool handler must execute"
    assert "should never run" in chunk.content[0].text
