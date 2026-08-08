"""P1 acceptance tests: streaming LLM requests must carry an LLM span.

Regression for the review finding that only non-streaming LLM calls created
CLIENT/LLM spans and injected traceparent, while streaming paths bypassed the
span entirely.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from map_core.utils import llm_engine as llm_engine_module
from map_core.utils.llm_engine import LLMEngine


def _install_tracer(monkeypatch) -> InMemorySpanExporter:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        llm_engine_module.otel_trace, "get_tracer", provider.get_tracer
    )
    return exporter


def _bare_engine(monkeypatch) -> LLMEngine:
    engine = object.__new__(LLMEngine)
    engine.config = SimpleNamespace(
        model="test-model",
        base_url="http://llm.test",
        max_retries=0,
    )
    engine.logger = SimpleNamespace(
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    monkeypatch.setattr(engine, "_prepare_messages", lambda msgs: msgs)
    monkeypatch.setattr(
        engine, "_prepare_params", lambda stream, **kwargs: {"stream": stream}
    )
    return engine


def test_async_stream_creates_llm_span_and_injects_traceparent(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    engine = _bare_engine(monkeypatch)

    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any):
        captured.update(kwargs)

        async def _chunks():
            return
            yield  # pragma: no cover

        return _chunks()

    engine._async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(
        engine,
        "_handle_async_stream",
        lambda response: _async_iter([{"type": "token", "data": "hi"}]),
    )

    async def scenario() -> list[Any]:
        gen = await engine._astream_once([{"role": "user", "content": "q"}])
        return [chunk async for chunk in gen]

    chunks = asyncio.run(scenario())

    assert chunks == [{"type": "token", "data": "hi"}]
    # traceparent propagated to the outbound LLM request
    headers = captured.get("extra_headers") or {}
    assert "traceparent" in headers

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert "stream test-model" in spans
    span = spans["stream test-model"]
    assert span.attributes["openinference.span.kind"] == "LLM"
    assert span.attributes["map.llm.call_kind"] == "stream"
    assert span.status.status_code != StatusCode.ERROR


async def _async_iter(items):
    for item in items:
        yield item


def test_async_stream_error_sets_span_error(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    engine = _bare_engine(monkeypatch)

    async def fake_create(**kwargs: Any):
        async def _chunks():
            return
            yield  # pragma: no cover

        return _chunks()

    engine._async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    async def broken_stream(response):
        yield {"type": "token", "data": "partial"}
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(engine, "_handle_async_stream", broken_stream)

    async def scenario() -> None:
        gen = await engine._astream_once([{"role": "user", "content": "q"}])
        async for _ in gen:
            pass

    try:
        asyncio.run(scenario())
    except RuntimeError:
        pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    span = spans["stream test-model"]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_async_stream_cancellation_still_ends_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    engine = _bare_engine(monkeypatch)

    async def fake_create(**kwargs: Any):
        async def _chunks():
            return
            yield  # pragma: no cover

        return _chunks()

    engine._async_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )

    async def endless_stream(response):
        index = 0
        while True:
            index += 1
            yield {"type": "token", "data": str(index)}

    monkeypatch.setattr(engine, "_handle_async_stream", endless_stream)

    async def scenario() -> None:
        gen = await engine._astream_once([{"role": "user", "content": "q"}])
        first = await gen.__anext__()
        assert first["data"] == "1"
        await gen.aclose()  # client disconnect / cancellation

    asyncio.run(scenario())

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert "stream test-model" in spans, "cancelled stream leaked an open span"


def test_sync_stream_creates_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    engine = _bare_engine(monkeypatch)

    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any):
        captured.update(kwargs)
        return iter(())

    engine._sync_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(engine, "_handle_sync_stream", lambda response: iter(["a", "b"]))

    gen = engine._stream_once([{"role": "user", "content": "q"}])
    assert list(gen) == ["a", "b"]

    headers = captured.get("extra_headers") or {}
    assert "traceparent" in headers
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert "stream test-model" in spans
