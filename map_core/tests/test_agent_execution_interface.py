"""PR-H1 acceptance tests for the public Agent Execution module.

These tests drive ``map_core.service.agent_execution`` exclusively through the
public caller-facing contract (AgentExecutionSpec / AgentRequest / AgentResult /
AgentActionEvent / AgentRuntime / hooks / cancel). They intentionally do not
import any AgentScope symbol; the engine stays behind the interface.
"""

from __future__ import annotations

import asyncio
from typing import Any

from map_core import config as app_config
from map_core.config.config_schema import LLMConfig
from map_core.schema.agent_schema import Function, ToolCall
from map_core.service.agent.skill_policy_checker import SkillPolicyChecker
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agent_execution import (
    AgentActionEvent,
    AgentExecutionHooks,
    AgentExecutionSpec,
    AgentRequest,
    AgentResult,
    AgentRuntime,
)
from map_core.utils.model_invocation import (
    ModelInvocationOutcome,
    ModelInvocationRequest,
)
from tests.model_invocation.scripted_provider import tool_outcome


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


class ScriptedLLM:
    """Deterministic ``invoke`` fake implementing the ModelInvocation surface."""

    def __init__(self, responses: list[ModelInvocationOutcome]) -> None:
        self.config = LLMConfig(
            base_url="http://localhost:9/v1",
            api_key="k",
            model="m",
        )
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def invoke(
        self, req: ModelInvocationRequest
    ) -> ModelInvocationOutcome:
        self.calls.append(_messages_to_dicts(req.messages))
        if not self._responses:
            raise AssertionError("ScriptedLLM script exhausted")
        return self._responses.pop(0)


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, function=Function(name=name, arguments=arguments))


def _search_tool(handler) -> Tool:
    return Tool(
        name="search",
        description="search the web",
        parameters={"type": "object", "properties": {}},
        handler=handler,
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


def _request(**overrides) -> AgentRequest:
    base = {"query": "hello", "staff_code": "tester"}
    base.update(overrides)
    return AgentRequest(**base)


def test_public_spec_has_no_engine_field() -> None:
    assert "engine" not in AgentExecutionSpec.model_fields
    spec = _spec()
    assert spec.name == "TestAgent"
    assert spec.tool_names == ["search"]


def test_execute_emits_ordered_actions_and_normalized_result() -> None:
    llm = ScriptedLLM(
        [
            tool_outcome(
                content="",
                tool_calls=[_tool_call("c1", "search", '{"query": "42"}')],
                usage={"prompt_tokens": 11, "completion_tokens": 4},
                finish_reason="tool_calls",
            ),
            tool_outcome(
                content="答案是 42",
                tool_calls=[_tool_call("c2", "terminate", '{"status": "success"}')],
                usage={"prompt_tokens": 20, "completion_tokens": 8},
                finish_reason="tool_calls",
            ),
        ]
    )
    executed: list[dict[str, Any]] = []

    async def search_handler(args, request, parid):
        executed.append({"args": args, "parid": parid})
        return {"content": "search says 42"}

    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": _search_tool(search_handler)},
    )
    action_events: list[AgentActionEvent] = []

    async def collect(event: AgentActionEvent) -> None:
        action_events.append(event)

    result = asyncio.run(
        runtime.execute(
            _spec(),
            _request(),
            action_handler=collect,
        )
    )

    # ordered action stream follows the public sequence
    actions = [event.action for event in action_events]
    assert actions[0] == "step_start"
    assert "tool_calls_selected" in actions
    assert "tool_call" in actions
    assert "tool_result" in actions
    assert "step_complete" in actions
    assert "terminate" in actions
    assert "final_answer" in actions
    assert actions.index("tool_call") < actions.index("tool_result")
    assert actions.index("step_start") < actions.index("final_answer")

    # tool executed exactly once through the governed tool path
    assert executed == [{"args": {"query": "42"}, "parid": executed[0]["parid"]}]

    # normalized terminal result carries content / usage / duration
    assert isinstance(result, AgentResult)
    assert result.success is True
    assert result.name == "TestAgent"
    assert "42" in (result.content or "")
    assert result.meta_data.get("agent_code") == "TestAgent"
    assert result.meta_data.get("duration_s") is not None
    assert result.meta_data["token_usage"] == {
        "prompt_tokens": 31,
        "completion_tokens": 12,
    }


def test_stream_yields_actions_and_result() -> None:
    llm = ScriptedLLM(
        [
            tool_outcome(
                content="直接回答",
                tool_calls=None,
                usage={"prompt_tokens": 5, "completion_tokens": 3},
                finish_reason="stop",
            )
        ]
    )
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": _search_tool(lambda args, request, parid: "unused")},
    )

    async def run():
        events: list[AgentActionEvent] = []
        results: list[AgentResult] = []
        async for item in runtime.stream([_spec()], _request()):
            if isinstance(item, AgentActionEvent):
                events.append(item)
            else:
                results.append(item)
        return events, results

    events, results = asyncio.run(run())
    assert events and events[0].action == "step_start"
    assert results and results[0].success is True
    assert results[0].content == "直接回答"


def test_memory_injection_and_writeback_via_public_module(monkeypatch) -> None:
    monkeypatch.setattr(
        app_config,
        "AGENT_MEMORY_ENABLED_AGENT_CODES",
        {"TestAgent"},
    )
    memory_history = [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    class FakeMemoryStore:
        def __init__(self) -> None:
            self.get_calls: list[dict[str, Any]] = []
            self.upsert_calls: list[dict[str, Any]] = []

        async def get_history(self, *, session_id, intention_id, agent_code, max_messages=20):
            self.get_calls.append(
                {
                    "session_id": session_id,
                    "intention_id": intention_id,
                    "agent_code": agent_code,
                }
            )
            return list(memory_history)

        async def upsert_history(self, *, session_id, intention_id, agent_code, history):
            self.upsert_calls.append(
                {
                    "session_id": session_id,
                    "intention_id": intention_id,
                    "agent_code": agent_code,
                    "history": history,
                }
            )

    llm = ScriptedLLM(
        [
            tool_outcome(
                content="本轮答案",
                tool_calls=[_tool_call("c1", "terminate", "{}")],
                usage={"prompt_tokens": 1, "completion_tokens": 2},
                finish_reason="tool_calls",
            )
        ]
    )
    memory = FakeMemoryStore()
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": _search_tool(lambda args, request, parid: "unused")},
        agent_memory_store=memory,
    )
    result = asyncio.run(
        runtime.execute(
            _spec(),
            _request(extra={"session_id": "sess-1", "request_id": "req-1"}),
        )
    )

    assert memory.get_calls == [
        {
            "session_id": "sess-1",
            "intention_id": "default",
            "agent_code": "TestAgent",
        }
    ]
    first_call_contents = [
        str(message.get("content")) for message in llm.calls[0]
    ]
    assert "上一轮问题" in first_call_contents
    assert "上一轮回答" in first_call_contents
    assert "本轮答案" not in first_call_contents  # history precedes current query

    assert result.success is True
    assert memory.upsert_calls, "memory writeback must happen after run"
    assert memory.upsert_calls[0]["session_id"] == "sess-1"
    assert memory.upsert_calls[0]["agent_code"] == "TestAgent"


def test_hooks_keep_existing_lifecycle_contract() -> None:
    llm = ScriptedLLM(
        [
            tool_outcome(
                content="done",
                tool_calls=None,
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="stop",
            )
        ]
    )
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": _search_tool(lambda args, request, parid: "unused")},
    )
    lifecycle: list[tuple[str, str, Any]] = []

    def on_start(agent, request):
        lifecycle.append(("start", getattr(agent, "name", "?"), request.query))

    def on_end(agent, status, data):
        lifecycle.append(("end", status, data))

    result = asyncio.run(
        runtime.execute(
            _spec(),
            _request(),
            hooks=AgentExecutionHooks(
                on_agent_start=on_start,
                on_agent_end=on_end,
            ),
        )
    )

    assert result.success is True
    assert lifecycle[0][0] == "start"
    assert lifecycle[0][1] == "TestAgent"
    assert lifecycle[1][0] == "end"
    assert lifecycle[1][1] == "success"
    assert lifecycle[1][2]["output"]["name"] == "TestAgent"


def test_tool_policy_denial_via_public_module() -> None:
    llm = ScriptedLLM(
        [
            tool_outcome(
                content="",
                tool_calls=[_tool_call("c1", "search", "{}")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="tool_calls",
            ),
            tool_outcome(
                content="policy handled",
                tool_calls=[_tool_call("c2", "terminate", "{}")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="tool_calls",
            ),
        ]
    )
    handler_called: list[bool] = []

    async def search_handler(args, request, parid):
        handler_called.append(True)
        return "should never run"

    runtime = AgentRuntime(
        llm=llm,
        tool_registry={"search": _search_tool(search_handler)},
    )
    events: list[AgentActionEvent] = []

    async def collect(event: AgentActionEvent) -> None:
        events.append(event)

    result = asyncio.run(
        runtime.execute(
            _spec(),
            _request(
                extra={
                    SkillPolicyChecker.RUNTIME_SWITCH_KEY: True,
                    SkillPolicyChecker.ALLOWED_TOOLS_KEY: ["another_tool"],
                }
            ),
            action_handler=collect,
        )
    )

    assert not handler_called, "denied tool handler must not execute"
    assert result.success is True  # the agent still finishes via terminate
    denial_events = [
        event for event in events if event.action == "tool_result"
    ]
    assert denial_events
    payload = denial_events[0].payload
    assert payload["tool_name"] == "search"
    assert payload["policy"]["allowed"] is False
    assert payload["policy"]["reason"] == "tool_not_in_allowed_tools"


def test_cancel_preset_returns_cancelled_without_llm_call() -> None:
    llm = ScriptedLLM([])
    runtime = AgentRuntime(llm=llm, tool_registry={})
    cancel = asyncio.Event()
    cancel.set()

    result = asyncio.run(
        runtime.execute(
            _spec(tool_names=[]),
            _request(),
            cancel=cancel,
        )
    )

    assert result.success is False
    assert result.error == "cancelled"
    assert result.meta_data.get("cancelled") is True
    assert llm.calls == []


def test_cancel_after_step_start_stops_before_model_call() -> None:
    llm = ScriptedLLM(
        [
            tool_outcome(
                content="",
                tool_calls=[_tool_call("c1", "search", "{}")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="tool_calls",
            )
        ]
    )
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={
            "search": _search_tool(
                lambda args, request, parid: {"content": "unreachable"}
            )
        },
    )
    cancel = asyncio.Event()
    events: list[AgentActionEvent] = []

    async def collect(event: AgentActionEvent) -> None:
        events.append(event)
        if event.action == "step_start":
            cancel.set()

    result = asyncio.run(
        runtime.execute(
            _spec(),
            _request(),
            action_handler=collect,
            cancel=cancel,
        )
    )

    assert result.success is False
    assert result.error == "cancelled"
    assert result.meta_data.get("cancelled") is True
    # the model call was skipped because cancel was set before it started
    assert llm.calls == []
    assert [event.action for event in events] == ["step_start"]


def test_max_steps_exit_contract_via_public_module() -> None:
    llm = ScriptedLLM(
        [
            tool_outcome(
                content="",
                tool_calls=[_tool_call("c1", "search", "{}")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="tool_calls",
            ),
            tool_outcome(
                content="",
                tool_calls=[_tool_call("c2", "search", "{}")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                finish_reason="tool_calls",
            ),
        ]
    )
    runtime = AgentRuntime(
        llm=llm,
        tool_registry={
            "search": _search_tool(
                lambda args, request, parid: {"content": "ok"}
            )
        },
    )
    events: list[AgentActionEvent] = []

    async def collect(event: AgentActionEvent) -> None:
        events.append(event)

    result = asyncio.run(
        runtime.execute(
            _spec(max_steps=2),
            _request(),
            action_handler=collect,
        )
    )

    assert [event.action for event in events].count("max_steps") == 1
    assert result.exit == {
        "reason": "max_steps",
        "step": 2,
        "had_tool_calls": True,
        "max_steps": 2,
    }
    assert result.success is True
    assert result.meta_data["token_usage"]["prompt_tokens"] == 2
    assert result.meta_data["token_usage"]["completion_tokens"] == 2
