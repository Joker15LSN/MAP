"""P2-5.3 acceptance tests: single TOOL span owner + unified error mapping.

Regression for the review finding that MapToolAdapter and ToolExecutor each
created a TOOL span for the same invocation, and that only timeouts set
``ToolChunk.state`` to ERROR while policy denials / business failures were
still reported as SUCCESS.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentscope.message import ToolResultState
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.service.agent.base import AgentRequest, ExecutionResult
from map_core.service.agent.skill_policy_checker import SkillPolicyChecker
from map_core.service.agent.tool_executor import classify_tool_result
from map_core.service.agent.tool_runtime import Tool
from map_core.service.agentscope2.agent import AgentScopeSceneAgent
from map_core.service.agentscope2.tool import MapToolAdapter


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"


class FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeLLMConfig()


class FakeStateStore:
    async def record_event(self, *, state_id, event_type, payload):
        return None


def _install_tracer(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel_trace, "get_tracer", provider.get_tracer)
    return exporter


def _build_adapter(
    monkeypatch, handler, allowed: bool
) -> tuple[MapToolAdapter, Any, InMemorySpanExporter]:
    exporter = _install_tracer(monkeypatch)
    tool = Tool(
        name="demo_tool",
        description="demo",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    agent = AgentScopeSceneAgent(
        llm=FakeLLM(),
        name="TestAgent",
        system_prompt="test",
        additional_user_prompt="",
        tools=[tool],
        max_steps=3,
        force_tool_call=False,
        scene_post_summary=None,
    )
    agent.set_execution_context(FakeStateStore(), "state-1")
    request = AgentRequest(
        query="run demo",
        staff_code="tester",
        extra={
            SkillPolicyChecker.RUNTIME_SWITCH_KEY: True,
            SkillPolicyChecker.ALLOWED_TOOLS_KEY: (
                ["demo_tool"] if allowed else ["other_tool"]
            ),
        },
    )
    adapter = MapToolAdapter(
        tool=tool,
        function_schema=tool.to_openai_tool(request, owner_agent_name=agent.name),
        owner=agent,
        request=request,
    )
    return adapter, request, exporter


def _tool_spans(exporter: InMemorySpanExporter):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("openinference.span.kind") == "TOOL"
    ]


def test_single_tool_span_per_invocation(monkeypatch) -> None:
    def handler(args, request, parid):
        return "ok"

    adapter, _request, exporter = _build_adapter(monkeypatch, handler, allowed=True)

    async def run():
        chunk = await adapter.call()
        await asyncio.sleep(0)
        return chunk

    chunk = asyncio.run(run())
    assert chunk.state == ToolResultState.SUCCESS

    spans = _tool_spans(exporter)
    assert len(spans) == 1, "adapter and executor must not both create TOOL spans"
    span = spans[0]
    assert span.name == "tool demo_tool"
    assert span.attributes["map.engine"] == "agentscope"
    assert span.attributes["map.tool.success"] is True


def test_policy_denial_marks_error_state(monkeypatch) -> None:
    def handler(args, request, parid):
        raise AssertionError("denied tool must not run")

    adapter, _request, exporter = _build_adapter(monkeypatch, handler, allowed=False)

    async def run():
        chunk = await adapter.call()
        await asyncio.sleep(0)
        return chunk

    chunk = asyncio.run(run())
    assert chunk.state == ToolResultState.ERROR
    assert "tool_forbidden" in chunk.content[0].text

    spans = _tool_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["map.tool.success"] is False
    assert span.status.status_code.name == "ERROR"


def test_business_failure_marks_error_state(monkeypatch) -> None:
    def handler(args, request, parid):
        return ExecutionResult(success=False, error="downstream empty")

    adapter, _request, exporter = _build_adapter(monkeypatch, handler, allowed=True)

    async def run():
        chunk = await adapter.call()
        await asyncio.sleep(0)
        return chunk

    chunk = asyncio.run(run())
    assert chunk.state == ToolResultState.ERROR

    spans = _tool_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["map.tool.success"] is False
    assert span.status.status_code.name == "ERROR"


def test_classify_tool_result_semantics() -> None:
    assert classify_tool_result({"content": "ok"}) == (True, "")
    assert classify_tool_result(ExecutionResult(content="ok")) == (True, "")

    success, reason = classify_tool_result(
        {"error": "tool denied by skill policy", "code": "tool_forbidden",
         "reason": "tool_not_in_allowed_tools"}
    )
    assert success is False
    assert "policy denied" in reason

    success, reason = classify_tool_result({"error": "tool timeout"})
    assert success is False
    assert reason == "tool timeout"

    success, reason = classify_tool_result(
        ExecutionResult(success=False, error="boom")
    )
    assert success is False
    assert reason == "boom"

    success, reason = classify_tool_result(ExecutionResult(success=True))
    assert success is True
