"""P2-1 acceptance tests for the AgentScope 2.0 adapter layer.

Covers: message block conversion, force_tool_call, terminate interception and
usage mapping as required by the migration plan.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from agentscope.message import (
    AssistantMsg,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)

from map_core.schema.agent_schema import Function, Message, ToolCall
from map_core.service.agentscope2.event import model_usage, reply_end_reason
from map_core.service.agentscope2.message import (
    agentscope_messages_to_openai,
    map_history_to_agentscope,
    message_reasoning,
    message_text,
)
from map_core.service.agentscope2.model import MapChatModelAdapter
from map_core.utils.llm_engine import ToolCallResponse


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"


class FakeLLM:
    """Minimal stand-in for LLMEngine exposing the ask_tool contract."""

    def __init__(self, responses: list[ToolCallResponse]) -> None:
        self.config = _FakeLLMConfig()
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def ask_tool(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "kwargs": kwargs,
            }
        )
        return self._responses.pop(0)


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, function=Function(name=name, arguments=arguments))


# ---------------------------------------------------------------------------
# message block conversions
# ---------------------------------------------------------------------------


def test_agentscope_messages_to_openai_assistant_blocks() -> None:
    assistant = AssistantMsg(
        name="assistant",
        content=[
            ThinkingBlock(thinking="thinking it through"),
            TextBlock(text="final answer"),
            ToolCallBlock(id="call_1", name="search", input='{"q": "a"}'),
        ],
    )
    converted = agentscope_messages_to_openai([assistant])

    assert converted == [
        {
            "role": "assistant",
            "content": "final answer",
            "reasoning_content": "thinking it through",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "a"}'},
                }
            ],
        }
    ]


def test_agentscope_messages_to_openai_tool_result_and_hint() -> None:
    assistant = AssistantMsg(
        name="assistant",
        content=[
            TextBlock(text="before"),
            ToolResultBlock(
                id="call_1",
                name="search",
                output=[TextBlock(text="tool output")],
                state=ToolResultState.SUCCESS,
            ),
            HintBlock(hint="extra hint"),
        ],
    )
    converted = agentscope_messages_to_openai([assistant])

    assert converted[0] == {"role": "assistant", "content": "before"}
    assert converted[1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "tool output",
    }
    assert converted[2] == {"role": "user", "content": "extra hint"}


def test_map_history_to_agentscope_roundtrip() -> None:
    history = [
        Message(role="system", content="you are map"),
        Message(role="user", content="hello"),
        Message(
            role="assistant",
            content="calling tool",
            tool_calls=[_tool_call("call_9", "query_db", '{"sql": "x"}')],
        ),
        Message(role="tool", name="query_db", tool_call_id="call_9", content="rows"),
    ]

    msgs = map_history_to_agentscope(history)

    assert msgs[0].role == "system"
    assert message_text(msgs[0]) == "you are map"
    assert msgs[1].role == "user"
    assistant = msgs[2]
    assert assistant.role == "assistant"
    # the trailing tool message must be merged into the assistant message
    assert len(msgs) == 3
    blocks = assistant.content
    assert isinstance(blocks[0], TextBlock) and blocks[0].text == "calling tool"
    assert isinstance(blocks[1], ToolCallBlock)
    assert blocks[1].id == "call_9" and blocks[1].name == "query_db"
    assert isinstance(blocks[2], ToolResultBlock)
    assert blocks[2].id == "call_9"
    assert blocks[2].output == "rows"


def test_map_history_reasoning_content_becomes_thinking_block() -> None:
    msgs = map_history_to_agentscope(
        [
            {
                "role": "assistant",
                "content": "done",
                "reasoning_content": "chain of thought",
            }
        ]
    )
    assert msgs[0].role == "assistant"
    block = msgs[0].content[0]
    assert isinstance(block, ThinkingBlock)
    assert block.thinking == "chain of thought"
    assert message_reasoning(msgs[0]) == "chain of thought"


# ---------------------------------------------------------------------------
# MapChatModelAdapter: force_tool_call / terminate interception / usage
# ---------------------------------------------------------------------------


def _make_adapter(responses: list[ToolCallResponse], **kwargs):
    llm = FakeLLM(responses)
    adapter = MapChatModelAdapter(llm, **kwargs)
    return adapter, llm


def test_force_tool_call_first_round_required_then_auto() -> None:
    responses = [
        ToolCallResponse(content="", tool_calls=[_tool_call("c1", "search")]),
        ToolCallResponse(content="final", tool_calls=None),
    ]
    adapter, llm = _make_adapter(responses, force_tool_call=True)
    tools = [{"type": "function", "function": {"name": "search"}}]
    messages = [UserMsg(name="user", content="hi")]

    async def run() -> None:
        await adapter._call_api(adapter.model, list(messages), tools=tools)
        await adapter._call_api(adapter.model, list(messages), tools=tools)

    asyncio.run(run())

    assert llm.calls[0]["tool_choice"] == "required"
    assert llm.calls[1]["tool_choice"] == "auto"


def test_explicit_tool_choice_wins_over_force_tool_call() -> None:
    from agentscope.tool import ToolChoice

    responses = [ToolCallResponse(content="", tool_calls=None)]
    adapter, llm = _make_adapter(responses, force_tool_call=True)

    async def run() -> None:
        await adapter._call_api(
            adapter.model,
            [UserMsg(name="user", content="hi")],
            tools=[{"type": "function", "function": {"name": "search"}}],
            tool_choice=ToolChoice(mode="none"),
        )

    asyncio.run(run())
    assert llm.calls[0]["tool_choice"] == "none"


def test_terminate_call_intercepted_and_stripped() -> None:
    responses = [
        ToolCallResponse(
            content="任务已完成",
            tool_calls=[
                _tool_call("c1", "terminate", '{"status": "success"}'),
            ],
            finish_reason="tool_calls",
        )
    ]
    handler_seen: list[tuple[int, Any]] = []
    adapter, llm = _make_adapter(
        responses,
        response_handler=lambda idx, resp: handler_seen.append((idx, resp)),
    )

    async def run():
        return await adapter._call_api(
            adapter.model,
            [UserMsg(name="user", content="finish")],
            tools=[{"type": "function", "function": {"name": "terminate"}}],
        )

    result = asyncio.run(run())

    assert adapter.last_terminate_call is not None
    assert adapter.last_terminate_call.function.name == "terminate"
    assert adapter.last_terminate_response is not None
    # the raw tool calls must not leak into the AgentScope response blocks
    assert not any(isinstance(block, ToolCallBlock) for block in result.content)
    assert any(isinstance(block, TextBlock) for block in result.content)
    # response handler still observes the original MAP response (with calls)
    assert handler_seen and handler_seen[0][0] == 0
    assert handler_seen[0][1].tool_calls


def test_usage_mapping_prompt_completion_tokens() -> None:
    responses = [
        ToolCallResponse(
            content="answer",
            usage={"prompt_tokens": 11, "completion_tokens": 7},
            response_time=1.5,
            request_id="req-1",
            finish_reason="stop",
        )
    ]
    adapter, _ = _make_adapter(responses)

    async def run():
        return await adapter._call_api(
            adapter.model,
            [UserMsg(name="user", content="count")],
        )

    result = asyncio.run(run())

    assert result.usage is not None
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.id == "req-1"
    assert result.metadata["map_finish_reason"] == "stop"


def test_model_usage_and_reply_end_reason_helpers() -> None:
    end_event = SimpleNamespace(input_tokens=3, output_tokens=5)
    assert model_usage(end_event) == {"prompt_tokens": 3, "completion_tokens": 5}

    class _Reason:
        value = "max_iters"

    assert reply_end_reason(SimpleNamespace(finished_reason=_Reason())) == "max_iters"
    assert reply_end_reason(SimpleNamespace(finished_reason="completed")) == "completed"
