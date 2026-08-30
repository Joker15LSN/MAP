"""P1-4.2 acceptance tests rewritten on the public ModelInvocation seam.

Non-stream ``llm_call`` Mongo events must bind the real LLM span id (not the
parent request span). The old ``object.__new__`` + monkeypatch-private-methods
style is replaced by a scripted ``ModelProvider``.
"""

from __future__ import annotations

import asyncio

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.config.config_schema import LLMConfig
from map_core.service.execution_event import set_run_context
from map_core.utils.llm_trace_context import llm_trace_context
from map_core.utils.model_invocation import (
    ModelInvocation,
    ModelInvocationRequest,
    ProviderResponse,
)
from map_core.utils.model_invocation import engine as engine_module
from tests.model_invocation.scripted_provider import (
    ScriptedProvider,
    completion_payload,
)
from tests.run_context_utils import make_run_context_sink


def _config() -> LLMConfig:
    return LLMConfig(
        base_url="http://llm.test/v1",
        api_key="k",
        model="fake-model",
        max_retries=0,
    )


def _install_tracer(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(engine_module.otel_trace, "get_tracer", provider.get_tracer)
    return exporter


def _llm_spans(exporter: InMemorySpanExporter):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("openinference.span.kind") == "LLM"
    ]


async def _scenario(
    run_context,
    sink,
    exporter: InMemorySpanExporter,
    *,
    tools: bool,
) -> None:
    provider = ScriptedProvider(
        [
            ProviderResponse(
                payload=completion_payload(
                    content="done" if tools else "ok",
                    tool_calls=None,
                )
            )
        ]
    )
    invocation = ModelInvocation(_config(), provider=provider)
    request: dict = {"messages": [{"role": "user", "content": "hi"}]}
    if tools:
        request["tools"] = [{"type": "function", "function": {"name": "search"}}]

    with llm_trace_context(
        agent_code="TestAgent",
        agent_name="Test Agent",
        component="test",
        phase="test",
        step=0,
        call_kind="tool_selection" if tools else "chat",
    ):
        with otel_trace.get_tracer("test").start_as_current_span("request.parent"):
            with set_run_context(run_id=run_context.run_id):
                await invocation.invoke(ModelInvocationRequest(**request))
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _assert_event_binds_llm_span(
    sink, exporter: InMemorySpanExporter
) -> None:
    llm_spans = _llm_spans(exporter)
    assert len(llm_spans) == 1
    actual_span_id = format(llm_spans[0].context.span_id, "016x")
    parent_like = [
        span for span in exporter.get_finished_spans() if span.name == "request.parent"
    ]
    assert parent_like, "parent span must be exported"
    parent_span_id = format(parent_like[0].context.span_id, "016x")

    llm_events = [
        event for event in sink.events if event.type.startswith("model.invocation_")
    ]
    assert llm_events, "model.invocation_* event must be recorded"
    assert llm_events[0].data["span_id"] == actual_span_id
    assert llm_events[0].data["span_id"] != parent_span_id


def test_ainvoke_mongo_event_binds_actual_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    run_context, sink = make_run_context_sink()
    asyncio.run(_scenario(run_context, sink, exporter, tools=False))
    _assert_event_binds_llm_span(sink, exporter)


def test_ask_tool_mongo_event_binds_actual_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    run_context, sink = make_run_context_sink()
    asyncio.run(_scenario(run_context, sink, exporter, tools=True))
    _assert_event_binds_llm_span(sink, exporter)

    llm_events = [
        event for event in sink.events if event.type.startswith("model.invocation_")
    ]
    assert llm_events[0].data["call_kind"] == "tool_selection"
