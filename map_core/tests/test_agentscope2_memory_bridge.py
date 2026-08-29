"""P2-5 acceptance tests: session memory bridging across engines.

Both engines share AgentRuntime._build_execution_request, which injects
AgentMemoryStore history into the request; results are written back via
result.data_source.history. These tests verify the new engine honours both
directions.
"""

from __future__ import annotations

import asyncio
from typing import Any

from map_core import config as app_config
from map_core.config.config_schema import LLMConfig
from map_core.service.agent.base import AgentRequest
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agent_runtime import AgentExecutionSpec, AgentRuntime
from map_core.utils.model_invocation import (
    ModelInvocationOutcome,
    ModelInvocationRequest,
    ModelUsage,
)

MEMORY_HISTORY = [
    {"role": "user", "content": "上一轮问题"},
    {"role": "assistant", "content": "上一轮回答"},
]


class FakeMemoryStore:
    def __init__(self) -> None:
        self.get_calls: list[dict] = []
        self.upsert_calls: list[dict] = []

    async def get_history(self, *, session_id, intention_id, agent_code, max_messages=20):
        self.get_calls.append(
            {
                "session_id": session_id,
                "intention_id": intention_id,
                "agent_code": agent_code,
            }
        )
        return list(MEMORY_HISTORY)

    async def upsert_history(self, *, session_id, intention_id, agent_code, history):
        self.upsert_calls.append(
            {
                "session_id": session_id,
                "intention_id": intention_id,
                "agent_code": agent_code,
                "history": history,
            }
        )


def _messages_to_dicts(messages: Any) -> list[dict]:
    converted: list[dict] = []
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


class ScriptedLLM:
    def __init__(self) -> None:
        self.config = LLMConfig(
            base_url="http://localhost:9/v1",
            api_key="k",
            model="m",
        )
        self.calls: list[list[dict]] = []

    async def invoke(
        self, req: ModelInvocationRequest
    ) -> ModelInvocationOutcome:
        self.calls.append(_messages_to_dicts(req.messages))
        return ModelInvocationOutcome(
            status="succeeded",
            content="本轮答案",
            tool_calls=[
                {
                    "id": "call_mem",
                    "type": "function",
                    "function": {"name": "terminate", "arguments": "{}"},
                }
            ],
            usage=ModelUsage(prompt_tokens=1, completion_tokens=2),
            finish_reason="tool_calls",
            attempts=1,
            latency_ms=0.0,
        )


def _spec(**overrides) -> AgentExecutionSpec:
    base = {
        "name": "TestAgent",
        "system_prompt": "be helpful",
        "tool_names": ["search"],
        "max_steps": 2,
    }
    base.update(overrides)
    return AgentExecutionSpec(**base)


def test_memory_bridge_inject_and_writeback(monkeypatch) -> None:
    monkeypatch.setattr(
        app_config,
        "AGENT_MEMORY_ENABLED_AGENT_CODES",
        {"TestAgent"},
    )
    llm = ScriptedLLM()
    memory = FakeMemoryStore()
    tool = Tool(
        name="search",
        description="d",
        parameters={},
        handler=lambda args, request, parid: "ok",
    )
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": tool},
        agent_memory_store=memory,
    )
    agent = runtime.build_agent(_spec(engine="agentscope"))
    request = AgentRequest(
        query="本轮问题",
        staff_code="t",
        extra={"session_id": "sess-1", "request_id": "req-1"},
    )

    result = asyncio.run(runtime.run_agent(agent, request, action_handler=None))

    # injection: the LLM saw the memory history ahead of the current query
    assert memory.get_calls == [
        {
            "session_id": "sess-1",
            "intention_id": "default",
            "agent_code": "TestAgent",
        }
    ]
    first_call_contents = [
        message.get("content") for message in llm.calls[0]
    ]
    assert "上一轮问题" in first_call_contents
    assert "上一轮回答" in first_call_contents
    assert "本轮问题" in first_call_contents

    # writeback: result data_source carries the session history for upsert
    assert result.success is True
    assert memory.upsert_calls, "memory writeback must happen after run"
    upsert = memory.upsert_calls[0]
    assert upsert["session_id"] == "sess-1"
    assert upsert["agent_code"] == "TestAgent"
    assert isinstance(upsert["history"], list) and upsert["history"]


def test_memory_disabled_by_config_skips_store(monkeypatch) -> None:
    monkeypatch.setattr(
        app_config,
        "AGENT_MEMORY_ENABLED_AGENT_CODES",
        set(),
    )
    llm = ScriptedLLM()
    memory = FakeMemoryStore()
    tool = Tool(name="search", description="d", parameters={})
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": tool},
        agent_memory_store=memory,
    )
    agent = runtime.build_agent(_spec(engine="agentscope"))
    request = AgentRequest(
        query="本轮问题",
        staff_code="t",
        extra={"session_id": "sess-1"},
    )

    asyncio.run(runtime.run_agent(agent, request, action_handler=None))

    assert not memory.get_calls
    assert not memory.upsert_calls
