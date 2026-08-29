"""P2-3 acceptance tests: end-to-end run of AgentScopeSceneAgent.

Drives the full ReAct loop with a scripted FakeLLM and verifies the emitted
action events match the MAP SSE contract (step_start / tool_calls_selected /
tool_call / tool_result / step_complete / terminate) and the final AgentResult
contract for both the terminate and direct-final-answer exits.
"""

from __future__ import annotations

import asyncio
from typing import Any

from map_core.schema.agent_schema import Function, ToolCall
from map_core.service.agent.base import AgentRequest
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agentscope2.agent import AgentScopeSceneAgent
from map_core.utils.model_invocation import (
    ModelInvocationOutcome,
    ModelInvocationRequest,
)
from tests.model_invocation.scripted_provider import tool_outcome


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"
    temperature = 0.0


def _messages_to_dicts(messages: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            converted.append(dict(message))
        else:
            model_dump = getattr(message, "model_dump", None)
            if callable(model_dump):
                converted.append(model_dump(exclude_none=True))
            else:
                converted.append(
                    {
                        "role": getattr(message, "role", None),
                        "content": getattr(message, "content", None),
                    }
                )
    return converted


class FakeLLM:
    def __init__(self, responses: list[ModelInvocationOutcome]) -> None:
        self.config = _FakeLLMConfig()
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def invoke(
        self, req: ModelInvocationRequest
    ) -> ModelInvocationOutcome:
        self.calls.append(_messages_to_dicts(req.messages))
        return self._responses.pop(0)


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, function=Function(name=name, arguments=arguments))


def _build_agent(llm: FakeLLM, action_events: list[Any]):
    executed: list[dict[str, Any]] = []

    async def search_handler(args, request, parid):
        executed.append({"args": args, "parid": parid})
        return {"content": "search says 42", "data_source": {"rows": [1, 2]}}

    tool = Tool(
        name="search",
        description="search the web",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=search_handler,
    )
    agent = AgentScopeSceneAgent(
        llm=llm,
        name="TestAgent",
        system_prompt="you are a test agent, {current_datetime}",
        additional_user_prompt="",
        tools=[tool],
        max_steps=3,
        force_tool_call=False,
        scene_post_summary=None,
    )
    agent.set_action_handler(lambda event: action_events.append(event))
    return agent, executed


def test_run_tool_call_then_terminate_matches_sse_contract() -> None:
    llm = FakeLLM(
        [
            tool_outcome(
                content="",
                tool_calls=[_tool_call("call_1", "search", '{"query": "meaning"}')],
                usage={"prompt_tokens": 10, "completion_tokens": 4},
                finish_reason="tool_calls",
            ),
            tool_outcome(
                content="答案是 42",
                tool_calls=[_tool_call("call_2", "terminate", '{"status": "success"}')],
                usage={"prompt_tokens": 20, "completion_tokens": 8},
                finish_reason="tool_calls",
            ),
        ]
    )
    action_events: list[Any] = []
    agent, executed = _build_agent(llm, action_events)
    request = AgentRequest(
        query="what is the meaning of life",
        staff_code="tester",
        extra={"request_id": "req-e2e"},
    )

    result = asyncio.run(agent.run(request))

    # tool was executed exactly once through the MAP governance path
    assert len(executed) == 1
    assert executed[0]["args"] == {"query": "meaning"}

    # SSE action contract
    actions = [event.action for event in action_events]
    assert actions[0] == "step_start"
    assert "tool_calls_selected" in actions
    assert "tool_call" in actions
    assert "tool_result" in actions
    assert "step_complete" in actions
    assert "terminate" in actions

    # AgentResult contract on terminate exit
    assert result.success is True
    assert result.name == "TestAgent"
    assert "42" in (result.content or "")

    # token usage accumulated from both model calls
    assert agent.token_usage["prompt_tokens"] == 30
    assert agent.token_usage["completion_tokens"] == 12

    # history fed back to the LLM contains the tool round-trip
    second_call_messages = llm.calls[1]
    roles = [message.get("role") for message in second_call_messages]
    assert "tool" in roles
    tool_message = next(m for m in second_call_messages if m.get("role") == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    assert "search says 42" in tool_message["content"]


def test_run_direct_final_answer_exit() -> None:
    llm = FakeLLM(
        [
            tool_outcome(
                content="直接回答，无需工具",
                tool_calls=None,
                usage={"prompt_tokens": 5, "completion_tokens": 3},
                finish_reason="stop",
            )
        ]
    )
    action_events: list[Any] = []
    agent, executed = _build_agent(llm, action_events)
    request = AgentRequest(query="just answer me", staff_code="tester")

    result = asyncio.run(agent.run(request))

    assert not executed
    assert result.success is True
    assert result.content == "直接回答，无需工具"
    actions = [event.action for event in action_events]
    assert actions and actions[0] == "step_start"
    assert "tool_call" not in actions


def test_run_max_steps_exit_marks_failure_without_tool_success() -> None:
    llm = FakeLLM(
        [
            tool_outcome(
                content="",
                tool_calls=[_tool_call("call_1", "terminate", "{}")],
            )
        ]
    )
    action_events: list[Any] = []
    agent, _ = _build_agent(llm, action_events)

    # force the max_steps branch by simulating the framework event directly
    request = AgentRequest(query="loop forever", staff_code="tester")

    # emit the max_steps action through the public event path
    async def run_with_event():
        agent._reset_run_state()
        agent.parid = "-"
        prepared = agent.exit_handler.prepare_request_for_execution(request)
        from map_core.service.agent.tool_call_session import ToolCallSession

        agent._compat_session = ToolCallSession.from_request(
            request_query=prepared.query,
            history=prepared.history,
            history_normalizer=agent._normalize_history,
            additional_user_prompt=agent.additional_user_prompt,
        )
        from agentscope.event import ExceedMaxItersEvent

        await agent._handle_event(
            ExceedMaxItersEvent(reply_id="reply_1", name=agent.name)
        )
        return await agent._finalize_result(prepared)

    result = asyncio.run(run_with_event())

    actions = [event.action for event in action_events]
    assert "max_steps" in actions
    # exit contract: reason is recorded even when content fallback succeeds
    assert result.exit["reason"] == "max_steps"
    assert result.exit["had_tool_calls"] is False
