"""Re-review P1-4.1 acceptance tests: timeout/cancellation must fail TOOL spans.

Regression for the finding that ``asyncio.wait_for`` timeouts surface inside
``execute_tool`` as ``CancelledError`` (a BaseException on Python 3.11+), so
the TOOL span ended UNSET without ``map.tool.success=false`` — on both the
AgentScope adapter path and the legacy concurrent path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from agentscope.message import ToolResultState
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.service.agent.base import AgentRequest
from map_core.service.agent.tool_executor import ToolExecutor
from map_core.service.agent.tool_runtime import Tool, ToolSet
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


class FakeStateStore:
    async def record_event(self, *, state_id, event_type, payload):
        return None


def _install_tracer(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel_trace, "get_tracer", provider.get_tracer)
    return exporter


def _tool_spans(exporter: InMemorySpanExporter):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("openinference.span.kind") == "TOOL"
    ]


def test_agentscope_adapter_timeout_marks_span_error(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)

    async def slow_handler(args, request, parid):
        await asyncio.sleep(5)
        return "never"

    tool = Tool(
        name="slow_tool",
        description="slow",
        parameters={"type": "object", "properties": {}},
        handler=slow_handler,
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
        tools_timeout=0.05,
    )
    request = AgentRequest(query="run slow tool", staff_code="tester")
    adapter = MapToolAdapter(
        tool=tool,
        function_schema=tool.to_openai_tool(request, owner_agent_name=agent.name),
        owner=agent,
        request=request,
    )

    run_context, _sink = make_run_context_sink()

    async def run():
        with set_run_context(run_id=run_context.run_id):
            chunk = await adapter.call()
            await asyncio.sleep(0)
            return chunk

    chunk = asyncio.run(run())
    assert chunk.state == ToolResultState.ERROR

    spans = _tool_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes.get("map.tool.success") is False
    assert span.status.status_code.name == "ERROR"


def test_legacy_concurrent_timeout_marks_span_error(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)

    async def slow_handler(args, request, parid):
        await asyncio.sleep(5)
        return "never"

    tool = Tool(
        name="slow_tool",
        description="slow",
        parameters={"type": "object", "properties": {}},
        handler=slow_handler,
    )
    recorded_results: list[Any] = []
    owner = SimpleNamespace(
        name="LegacyAgent",
        agent_display_name="Legacy Agent",
        record_tool_result=lambda **kwargs: recorded_results.append(kwargs),
        record_tool_call=lambda **kwargs: None,
    )
    executor = ToolExecutor(
        owner=owner,
        toolset=ToolSet([tool], include_terminate=False),
        tools_timeout=0.05,
    )
    request = AgentRequest(query="run slow tool", staff_code="tester")
    call_obj = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="slow_tool", arguments="{}"),
    )

    async def run():
        return await executor.execute_concurrently(
            tool_calls=[call_obj],
            request=request,
            step=1,
            parid="par-1",
        )

    results, tool_called, _observations = asyncio.run(run())
    assert results["call-1"]["error"] == "tool timeout"

    spans = _tool_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes.get("map.tool.success") is False
    assert span.status.status_code.name == "ERROR"
    assert span.attributes.get("map.tool.call_id") == "call-1"
