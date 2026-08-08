"""P1 acceptance tests: event dispatcher must preserve OTel trace context.

Regression for the review finding that coroutines queued into the
EventDispatcher lost the request's contextvars, so Mongo events written by
dispatcher workers had no trace/span ids.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.service import state_store as state_store_module
from map_core.service.state_store import (
    BaseAgentStateHandler,
    GlobalAgentStateStore,
    fire_and_forget,
)


class _CapturingHandler(BaseAgentStateHandler):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def ensure_state(self, state_id: str, base_state: dict[str, Any]) -> None:
        return None

    async def handle_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict[str, Any],
        base_state: dict[str, Any] | None = None,
    ) -> None:
        # Yield control so the request span is already ended when the worker
        # runs — mirrors production where workers execute after the request.
        await asyncio.sleep(0)
        self.events.append(
            {"state_id": state_id, "event_type": event_type, "payload": payload}
        )

    async def close(self) -> None:
        return None


@pytest.fixture
def capture_store(monkeypatch):
    """A GlobalAgentStateStore with only a capturing handler."""
    monkeypatch.setattr(GlobalAgentStateStore, "_instance", None)
    store = GlobalAgentStateStore.instance()
    handler = _CapturingHandler()
    store.handlers = [handler]
    yield store, handler
    GlobalAgentStateStore._instance = None


def _install_tracer(monkeypatch) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    monkeypatch.setattr(otel_trace, "get_tracer", provider.get_tracer)
    return provider


def test_dispatcher_worker_events_keep_request_trace_id(
    monkeypatch, capture_store
) -> None:
    store, handler = capture_store
    provider = _install_tracer(monkeypatch)
    tracer = provider.get_tracer("test")

    async def scenario() -> None:
        store.start()
        with tracer.start_as_current_span("request.span"):
            expected = state_store_module.current_trace_context()
            assert expected  # sanity: inside the span the context is valid
            fire_and_forget(
                store.record_event(
                    state_id="s1",
                    event_type="tool_result",
                    payload={"tool": "demo"},
                )
            )
        # span ended here; the queued coroutine runs afterwards in a worker.
        # close() drains the queue, guaranteeing the worker processed it.
        await store.close()

    asyncio.run(scenario())
    assert len(handler.events) == 1
    payload = handler.events[0]["payload"]
    assert payload.get("_trace", {}).get("trace_id"), (
        "dispatcher worker lost the request trace context"
    )
    assert payload["_trace"].get("span_id")


def test_record_event_nowait_snapshots_trace_at_call_site(
    monkeypatch, capture_store
) -> None:
    store, handler = capture_store
    provider = _install_tracer(monkeypatch)
    tracer = provider.get_tracer("test")

    async def scenario() -> None:
        store.start()
        with tracer.start_as_current_span("request.span"):
            store.record_event_nowait(
                state_id="s1",
                event_type="agent_message",
                payload={"content": "hi"},
            )
        await store.close()

    asyncio.run(scenario())
    assert len(handler.events) == 1
    payload = handler.events[0]["payload"]
    assert payload.get("_trace", {}).get("trace_id")


def test_queue_full_drops_without_unclosed_coroutine_warning(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(GlobalAgentStateStore, "_instance", None)
    store = GlobalAgentStateStore.instance()
    store.handlers = [_CapturingHandler()]
    store._dispatcher._queue = asyncio.Queue(maxsize=1)

    async def scenario() -> None:
        store.start()
        for index in range(3):
            fire_and_forget(
                store.record_event(
                    state_id="s1",
                    event_type="request.start",
                    payload={"index": index},
                )
            )
        await asyncio.sleep(0)
        await store.close()

    asyncio.run(scenario())
    # no "coroutine was never awaited" warnings should surface
    captured = capsys.readouterr()
    assert "never awaited" not in captured.err
