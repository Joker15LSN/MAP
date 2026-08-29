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


class _CapturingStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record_event(self, *, state_id, event_type, payload):
        self.events.append((event_type, payload))


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
    store: _CapturingStore,
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
        state_store=store,
        state_id="state-1",
        agent_code="TestAgent",
        agent_name="Test Agent",
        component="test",
        phase="test",
        step=0,
        call_kind="tool_selection" if tools else "chat",
    ):
        with otel_trace.get_tracer("test").start_as_current_span("request.parent"):
            await invocation.invoke(ModelInvocationRequest(**request))
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _assert_event_binds_llm_span(
    store: _CapturingStore, exporter: InMemorySpanExporter
) -> None:
    llm_spans = _llm_spans(exporter)
    assert len(llm_spans) == 1
    actual_span_id = format(llm_spans[0].context.span_id, "016x")
    parent_like = [
        span for span in exporter.get_finished_spans() if span.name == "request.parent"
    ]
    assert parent_like, "parent span must be exported"
    parent_span_id = format(parent_like[0].context.span_id, "016x")

    llm_events = [payload for kind, payload in store.events if kind == "llm_call"]
    assert llm_events, "llm_call Mongo event must be recorded"
    assert llm_events[0]["span_id"] == actual_span_id
    assert llm_events[0]["span_id"] != parent_span_id


def test_ainvoke_mongo_event_binds_actual_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    store = _CapturingStore()
    asyncio.run(_scenario(store, exporter, tools=False))
    _assert_event_binds_llm_span(store, exporter)


def test_ask_tool_mongo_event_binds_actual_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    store = _CapturingStore()
    asyncio.run(_scenario(store, exporter, tools=True))
    _assert_event_binds_llm_span(store, exporter)

    llm_events = [payload for kind, payload in store.events if kind == "llm_call"]
    assert llm_events[0]["call_kind"] == "tool_selection"
