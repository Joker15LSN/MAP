"""Re-review P1-4.3 acceptance tests: BFF forms its own trace nodes.

Regression for the finding that the BFF only forwarded inbound W3C headers,
so a regular browser request (no traceparent) produced a trace starting at
map_core with no BFF SERVER/CLIENT nodes. Expected shape:

    BFF SERVER -> BFF CLIENT -> map_core SERVER -> ...
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.main import create_app
from app.schemas import AdminState
from app.telemetry import instrument_app


class _FakeStore:
    async def load(self) -> AdminState:
        return AdminState.default()


app = create_app(store=_FakeStore())

INBOUND_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INBOUND_SPAN_ID = "00f067aa0ba902b7"
INBOUND_TRACEPARENT = f"00-{INBOUND_TRACE_ID}-{INBOUND_SPAN_ID}-01"


@pytest.fixture(scope="module")
def exporter():
    in_memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_memory))
    instrument_app(app, provider)
    yield in_memory
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.uninstrument_app(app)
    HTTPXClientInstrumentor().uninstrument()


class _FakeUpstreamPool:
    """Stands in for the httpcore connection pool inside the transport."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def handle_async_request(self, request) -> Any:
        import httpcore

        self._captured["headers"] = {key.decode(): value.decode() for key, value in request.headers}
        return httpcore.Response(
            200,
            headers=[(b"content-type", b"application/json")],
            content=b'{"content": "ok", "meta": {}}',
        )

    async def aclose(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _InstrumentedFakeTransport(httpx.AsyncHTTPTransport):
    """AsyncHTTPTransport whose pool is fake but whose own
    ``handle_async_request`` is the real (instrumented) one.

    ``MockTransport``/``ASGITransport`` inherit ``AsyncBaseTransport`` and
    therefore bypass the opentelemetry-httpx wrap point entirely; going
    through ``AsyncHTTPTransport`` keeps the CLIENT span + traceparent
    injection on the real production code path.
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        object.__init__(self)  # skip real pool creation
        self._pool = _FakeUpstreamPool(captured)

    async def aclose(self) -> None:
        pass

    async def __aenter__(self):
        await self._pool.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._pool.__aexit__(*args)


@pytest.fixture()
def upstream_capture(monkeypatch):
    """Route MapCoreClient httpx calls through an in-process upstream."""
    captured: dict[str, Any] = {}

    instrumented_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = _InstrumentedFakeTransport(captured)
        return instrumented_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return captured


def _parse_traceparent(value: str) -> tuple[str, str]:
    parts = value.split("-")
    assert len(parts) == 4, f"malformed traceparent: {value}"
    return parts[1], parts[2]


def test_request_without_inbound_header_forms_bff_trace(exporter, upstream_capture) -> None:
    exporter.clear()
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"query": "hi"})
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind.name == "SERVER"]
    client_spans = [span for span in spans if span.kind.name == "CLIENT"]
    assert server_spans, "BFF must create a SERVER span for the request"
    assert any(
        "global_domain" in str(span.attributes.get("http.url", ""))
        or "global_domain" in str(span.attributes.get("url.full", ""))
        for span in client_spans
    ), "BFF must create a CLIENT span around the map_core call"

    server_span = server_spans[0]
    core_client_span = next(
        span
        for span in client_spans
        if "global_domain" in str(span.attributes.get("http.url", ""))
        or "global_domain" in str(span.attributes.get("url.full", ""))
    )
    # BFF CLIENT is a child of BFF SERVER, same trace
    assert core_client_span.context.trace_id == server_span.context.trace_id
    assert core_client_span.parent.span_id == server_span.context.span_id

    # outbound traceparent joins the map_core SERVER span to this trace
    trace_id, span_id = _parse_traceparent(upstream_capture["headers"]["traceparent"])
    assert trace_id == format(server_span.context.trace_id, "032x")
    assert span_id == format(core_client_span.context.span_id, "016x")


def test_request_with_inbound_header_continues_upstream_trace(exporter, upstream_capture) -> None:
    exporter.clear()
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"query": "hi"},
            headers={"traceparent": INBOUND_TRACEPARENT},
        )
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind.name == "SERVER"]
    assert server_spans
    server_span = server_spans[0]

    # SERVER span continues the inbound trace under the inbound parent
    assert format(server_span.context.trace_id, "032x") == INBOUND_TRACE_ID
    assert server_span.parent is not None
    assert format(server_span.parent.span_id, "016x") == INBOUND_SPAN_ID

    # outbound call still carries the same trace
    trace_id, _span_id = _parse_traceparent(upstream_capture["headers"]["traceparent"])
    assert trace_id == INBOUND_TRACE_ID
