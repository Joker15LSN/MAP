"""P1 acceptance tests rewritten on the public ModelInvocation seam.

Streaming LLM requests must carry an LLM span; the outbound request must carry
a W3C ``traceparent``; span status must reflect success/failure/cancellation.
The old ``object.__new__`` + monkeypatch-private-methods style is replaced by a
scripted provider (span lifecycle) and a fake AsyncOpenAI injected into the
real OpenAI-compatible adapter (traceparent propagation).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from map_core.config.config_schema import LLMConfig
from map_core.utils.llm_engine import LLMEngine
from map_core.utils.model_invocation import (
    ModelInvocation,
    ModelInvocationRequest,
    ProviderError,
    ProviderStream,
    openai_compatible,
)
from map_core.utils.model_invocation import engine as engine_module
from tests.model_invocation.scripted_provider import (
    ScriptedProvider,
    content_chunk,
    usage_chunk,
)


def _install_tracer(monkeypatch) -> InMemorySpanExporter:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(engine_module.otel_trace, "get_tracer", provider.get_tracer)
    return exporter


def _config() -> LLMConfig:
    return LLMConfig(
        base_url="http://llm.test/v1",
        api_key="k",
        model="test-model",
        max_retries=0,
    )


def _install_fake_openai(monkeypatch, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeCompletions:
        async def create(self, **kwargs: Any):
            captured.update(kwargs)

            async def _gen() -> AsyncIterator[dict[str, Any]]:
                for chunk in chunks:
                    yield chunk

            return _gen()

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = FakeChat()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(openai_compatible, "AsyncOpenAI", FakeClient)
    return captured


def _finished_spans(exporter: InMemorySpanExporter) -> dict[str, Any]:
    return {span.name: span for span in exporter.get_finished_spans()}


def test_async_stream_creates_llm_span_and_injects_traceparent(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    captured = _install_fake_openai(
        monkeypatch,
        [
            content_chunk("hi", model="test-model"),
            usage_chunk(),
        ],
    )

    async def scenario() -> None:
        adapter = openai_compatible.OpenAICompatibleProvider(_config())
        invocation = ModelInvocation(_config(), provider=adapter)
        stream = await invocation.invoke(
            ModelInvocationRequest(
                messages=[{"role": "user", "content": "q"}], stream=True
            )
        )
        events = [event async for event in stream]
        assert events[-1].status == "succeeded"

    asyncio.run(scenario())

    headers = captured.get("extra_headers") or {}
    assert "traceparent" in headers

    spans = _finished_spans(exporter)
    assert "stream test-model" in spans
    span = spans["stream test-model"]
    assert span.attributes["openinference.span.kind"] == "LLM"
    assert span.attributes["map.llm.call_kind"] == "stream"
    assert span.status.status_code != StatusCode.ERROR


def test_async_stream_error_sets_span_error(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)

    async def broken_chunks() -> AsyncIterator[dict[str, Any]]:
        yield content_chunk("partial")
        raise ProviderError("provider_error", "upstream exploded", True)

    provider = ScriptedProvider([ProviderStream(broken_chunks(), complete=False)])
    invocation = ModelInvocation(_config(), provider=provider)

    async def scenario() -> None:
        stream = await invocation.invoke(
            ModelInvocationRequest(
                messages=[{"role": "user", "content": "q"}], stream=True
            )
        )
        async for _ in stream:
            pass

    asyncio.run(scenario())

    spans = _finished_spans(exporter)
    span = spans["stream test-model"]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_async_stream_cancellation_still_ends_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)

    async def endless_chunks() -> AsyncIterator[dict[str, Any]]:
        index = 0
        while True:
            index += 1
            yield content_chunk(str(index))

    provider = ScriptedProvider([ProviderStream(endless_chunks(), complete=False)])
    invocation = ModelInvocation(_config(), provider=provider)

    async def scenario() -> None:
        stream = await invocation.invoke(
            ModelInvocationRequest(
                messages=[{"role": "user", "content": "q"}], stream=True
            )
        )
        first = await stream.__anext__()
        assert first.data is not None and first.data["data"] == "1"
        await stream.aclose()  # client disconnect / cancellation

    asyncio.run(scenario())

    spans = _finished_spans(exporter)
    assert "stream test-model" in spans, "cancelled stream leaked an open span"


def test_shell_sync_stream_creates_llm_span(monkeypatch) -> None:
    exporter = _install_tracer(monkeypatch)
    captured = _install_fake_openai(
        monkeypatch,
        [
            content_chunk("a", model="test-model"),
            content_chunk("b", model="test-model"),
            usage_chunk(),
        ],
    )

    engine = LLMEngine(_config())
    assert list(engine.stream([{"role": "user", "content": "q"}])) == ["a", "b"]

    headers = captured.get("extra_headers") or {}
    assert "traceparent" in headers
    spans = _finished_spans(exporter)
    assert "stream test-model" in spans
