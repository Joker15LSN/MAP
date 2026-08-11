"""Re-review P1-4.2 acceptance tests: non-stream Mongo events bind the real
LLM span id.

Regression for the finding that non-streaming ``chat`` / ``tool_selection``
calls recorded the Mongo ``llm_call`` event AFTER the LLM span context had
been exited, so the event carried the parent request span id instead of the
actual LLM span id.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.utils.llm_engine import LLMEngine
from map_core.utils.llm_trace_context import llm_trace_context


class _CapturingStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record_event(self, *, state_id, event_type, payload):
        self.events.append((event_type, payload))


class _FakeCompletions:
    async def create(self, *, messages, extra_headers=None, **params):
        return SimpleNamespace(ok=True)


def _bare_engine() -> LLMEngine:
    engine = object.__new__(LLMEngine)
    engine.config = SimpleNamespace(model="fake-model", base_url="http://llm.test/v1")
    engine.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    return engine


def _install_tracer(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel_trace, "get_tracer", provider.get_tracer)
    return exporter


def _llm_spans(exporter: InMemorySpanExporter):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("openinference.span.kind") == "LLM"
    ]


def _patch_engine(monkeypatch, engine: LLMEngine) -> None:
    monkeypatch.setattr(
        LLMEngine, "_prepare_messages", lambda self, msgs: [{"role": "user", "content": "hi"}]
    )
    monkeypatch.setattr(
        LLMEngine, "_prepare_params", lambda self, stream=False, **kw: {"model": "fake-model"}
    )


def test_ainvoke_mongo_event_binds_actual_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    engine = _bare_engine()
    _patch_engine(monkeypatch, engine)
    engine._async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions())
    )
    fake_response = SimpleNamespace(choices=[])
    monkeypatch.setattr(
        LLMEngine, "_coerce_chat_completion_response", lambda self, raw: fake_response
    )
    monkeypatch.setattr(
        LLMEngine,
        "_handle_async_response",
        lambda self, response, started_at=None: SimpleNamespace(
            content="ok", model="fake-model", usage=None, finish_reason="stop",
            response_time=0.0, request_id=None,
        ),
    )

    store = _CapturingStore()

    async def scenario():
        with llm_trace_context(
            state_store=store,
            state_id="state-1",
            agent_code="TestAgent",
            agent_name="Test Agent",
            component="test",
            phase="test",
            step=0,
            call_kind="chat",
        ):
            with otel_trace.get_tracer("test").start_as_current_span("request.parent"):
                await engine._ainvoke_once([{"role": "user", "content": "hi"}])
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

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


def test_ask_tool_mongo_event_binds_actual_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    engine = _bare_engine()
    _patch_engine(monkeypatch, engine)
    engine._async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions())
    )
    fake_message = SimpleNamespace(content="done", tool_calls=None)
    fake_response = SimpleNamespace(
        choices=[SimpleNamespace(message=fake_message, finish_reason="stop")],
        usage=None,
        id="resp-1",
        model="fake-model",
    )
    monkeypatch.setattr(
        LLMEngine, "_coerce_chat_completion_response", lambda self, raw: fake_response
    )
    monkeypatch.setattr(
        LLMEngine,
        "_handle_async_response",
        lambda self, response, started_at=None: SimpleNamespace(
            content="", model="fake-model", usage=None, finish_reason="stop",
            response_time=0.0, request_id=None,
        ),
    )

    store = _CapturingStore()

    async def scenario():
        with llm_trace_context(
            state_store=store,
            state_id="state-1",
            agent_code="TestAgent",
            agent_name="Test Agent",
            component="test",
            phase="test",
            step=0,
            call_kind="tool_selection",
        ):
            with otel_trace.get_tracer("test").start_as_current_span("request.parent"):
                await engine._ask_tool_once([{"role": "user", "content": "hi"}])
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    llm_spans = _llm_spans(exporter)
    assert len(llm_spans) == 1
    actual_span_id = format(llm_spans[0].context.span_id, "016x")

    llm_events = [payload for kind, payload in store.events if kind == "llm_call"]
    assert llm_events, "llm_call Mongo event must be recorded"
    assert llm_events[0]["span_id"] == actual_span_id
