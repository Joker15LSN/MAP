"""P1 acceptance tests: HTTP MCP tool calls must propagate W3C trace context.

Regression for the review finding that ``_call_http_mcp_tool`` only sent
user-configured headers, so the Tool span never reached MCP services.

S2-04: the call path is the guarded egress (post_json_stream_guarded) and
configured headers must come from secret references; these tests stub the
network layer and the egress policy to focus on W3C propagation semantics.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from map_core.service import dynamic_tools
from map_core.service.dynamic_tools import _call_http_mcp_tool
from map_core.service.mcp_egress import GuardedResponse


class _FakeGuardedPost:
    captured: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> GuardedResponse:
        _FakeGuardedPost.captured = dict(kwargs)
        return GuardedResponse(
            status_code=200,
            headers=httpx.Headers(),
            body=json.dumps(
                {"result": {"content": [{"type": "text", "text": "ok"}]}}
            ).encode(),
        )


def _allow_egress(monkeypatch) -> None:
    """Neutralize the S2-04 address policy (covered by its own tests)."""
    monkeypatch.setattr(
        dynamic_tools, "validate_mcp_url",
        lambda url, policy: ([], frozenset()),
    )
    monkeypatch.setattr(
        dynamic_tools,
        "EgressPolicy",
        SimpleNamespace(
            from_env=lambda: SimpleNamespace(max_response_bytes=1024 * 1024)
        ),
    )
    fake = _FakeGuardedPost()
    monkeypatch.setattr(dynamic_tools, "post_json_stream_guarded", fake)


def _install_tracer(monkeypatch) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    monkeypatch.setattr(otel_trace, "get_tracer", provider.get_tracer)
    return provider


def test_mcp_http_call_injects_traceparent(monkeypatch) -> None:
    _allow_egress(monkeypatch)
    provider = _install_tracer(monkeypatch)
    tracer = provider.get_tracer("test")

    async def scenario() -> None:
        with tracer.start_as_current_span("tool.span"):
            result = await _call_http_mcp_tool(
                server={"url": "https://mcp.test/rpc"},
                tool_name="demo_tool",
                args={"x": 1},
            )
            assert result.success is True

    asyncio.run(scenario())

    headers = _FakeGuardedPost.captured["headers"]
    assert "traceparent" in headers
    assert headers["traceparent"].startswith("00-")


def test_mcp_http_call_preserves_business_headers_and_overrides_static_traceparent(
    monkeypatch,
) -> None:
    _allow_egress(monkeypatch)
    monkeypatch.setenv("MAP_TEST_AUTH", "Bearer user-token")
    monkeypatch.setenv(
        "MAP_TEST_STATIC_TP", "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    )
    provider = _install_tracer(monkeypatch)
    tracer = provider.get_tracer("test")

    async def scenario() -> None:
        with tracer.start_as_current_span("tool.span"):
            await _call_http_mcp_tool(
                server={
                    "url": "https://mcp.test/rpc",
                    "headers": {
                        "Authorization": "${ENV:MAP_TEST_AUTH}",
                        "traceparent": "${ENV:MAP_TEST_STATIC_TP}",
                    },
                },
                tool_name="demo_tool",
                args={},
            )

    asyncio.run(scenario())

    headers = _FakeGuardedPost.captured["headers"]
    # business headers preserved (resolved from their secret ref)
    assert headers["Authorization"] == "Bearer user-token"
    # statically configured traceparent must NOT survive: it would pin the
    # call to a stale trace. The dynamic one references the active span.
    assert headers["traceparent"] != "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert headers["traceparent"].startswith("00-")


def test_mcp_http_call_without_active_span_drops_static_traceparent(monkeypatch) -> None:
    _allow_egress(monkeypatch)
    monkeypatch.setenv(
        "MAP_TEST_STATIC_TP", "00-" + "c" * 32 + "-" + "d" * 16 + "-01"
    )

    async def scenario() -> None:
        await _call_http_mcp_tool(
            server={
                "url": "https://mcp.test/rpc",
                "headers": {"traceparent": "${ENV:MAP_TEST_STATIC_TP}"},
            },
            tool_name="demo_tool",
            args={},
        )

    asyncio.run(scenario())
    headers = _FakeGuardedPost.captured["headers"]
    assert "traceparent" not in headers


def test_mcp_http_call_without_active_span_has_no_traceparent(monkeypatch) -> None:
    _allow_egress(monkeypatch)

    async def scenario() -> None:
        await _call_http_mcp_tool(
            server={"url": "https://mcp.test/rpc"},
            tool_name="demo_tool",
            args={},
        )

    asyncio.run(scenario())
    headers = _FakeGuardedPost.captured["headers"]
    assert "traceparent" not in headers
