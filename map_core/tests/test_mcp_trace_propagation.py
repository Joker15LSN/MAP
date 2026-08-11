"""P1 acceptance tests: HTTP MCP tool calls must propagate W3C trace context.

Regression for the review finding that ``_call_http_mcp_tool`` only sent
user-configured headers, so the Tool span never reached MCP services.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.service import dynamic_tools
from map_core.service.dynamic_tools import _call_http_mcp_tool


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}


class _FakeAsyncClient:
    captured: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        _FakeAsyncClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeResponse()


def _install_tracer(monkeypatch) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    monkeypatch.setattr(otel_trace, "get_tracer", provider.get_tracer)
    return provider


def test_mcp_http_call_injects_traceparent(monkeypatch) -> None:
    provider = _install_tracer(monkeypatch)
    monkeypatch.setattr(dynamic_tools.httpx, "AsyncClient", _FakeAsyncClient)
    tracer = provider.get_tracer("test")

    async def scenario() -> None:
        with tracer.start_as_current_span("tool.span"):
            result = await _call_http_mcp_tool(
                server={"url": "http://mcp.test/rpc"},
                tool_name="demo_tool",
                args={"x": 1},
            )
            assert result.success is True

    asyncio.run(scenario())

    headers = _FakeAsyncClient.captured["headers"]
    assert "traceparent" in headers
    assert headers["traceparent"].startswith("00-")


def test_mcp_http_call_preserves_business_headers_and_overrides_static_traceparent(
    monkeypatch,
) -> None:
    provider = _install_tracer(monkeypatch)
    monkeypatch.setattr(dynamic_tools.httpx, "AsyncClient", _FakeAsyncClient)
    tracer = provider.get_tracer("test")

    async def scenario() -> None:
        with tracer.start_as_current_span("tool.span"):
            await _call_http_mcp_tool(
                server={
                    "url": "http://mcp.test/rpc",
                    "headers": {
                        "Authorization": "Bearer user-token",
                        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
                    },
                },
                tool_name="demo_tool",
                args={},
            )

    asyncio.run(scenario())

    headers = _FakeAsyncClient.captured["headers"]
    # business headers preserved
    assert headers["Authorization"] == "Bearer user-token"
    # statically configured traceparent must NOT survive: it would pin the
    # call to a stale trace. The dynamic one references the active span.
    assert headers["traceparent"] != "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert headers["traceparent"].startswith("00-")


def test_mcp_http_call_without_active_span_drops_static_traceparent(monkeypatch) -> None:
    monkeypatch.setattr(dynamic_tools.httpx, "AsyncClient", _FakeAsyncClient)

    async def scenario() -> None:
        await _call_http_mcp_tool(
            server={
                "url": "http://mcp.test/rpc",
                "headers": {"traceparent": "00-" + "c" * 32 + "-" + "d" * 16 + "-01"},
            },
            tool_name="demo_tool",
            args={},
        )

    asyncio.run(scenario())
    headers = _FakeAsyncClient.captured["headers"]
    assert "traceparent" not in headers


def test_mcp_http_call_without_active_span_has_no_traceparent(monkeypatch) -> None:
    monkeypatch.setattr(dynamic_tools.httpx, "AsyncClient", _FakeAsyncClient)

    async def scenario() -> None:
        await _call_http_mcp_tool(
            server={"url": "http://mcp.test/rpc"},
            tool_name="demo_tool",
            args={},
        )

    asyncio.run(scenario())
    headers = _FakeAsyncClient.captured["headers"]
    assert "traceparent" not in headers
