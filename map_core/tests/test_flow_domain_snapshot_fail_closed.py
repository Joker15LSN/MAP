"""Step 7 PR-J6: FlowDomain must fail closed on runtime snapshot errors.

When the pinned snapshot provider raises any ``RuntimeSnapshotError``, the
stream must yield only start + error (no done) and must NEVER fall through
to the global-domain fallback. Router-injected runtime snapshot headers
must reach the provider with the validated values.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from map_core.routers import flow_domain_router
from map_core.schema.flow_domain_schema import FlowChatRequest
from map_core.service.flow_domain import FlowDomain
from map_core.service.runtime_snapshot_transport import (
    RuntimeSnapshotAuthError,
    RuntimeSnapshotDigestMismatchError,
    RuntimeSnapshotError,
    RuntimeSnapshotIdMissingError,
    RuntimeSnapshotNotFoundError,
    RuntimeSnapshotSchemaError,
)

SNAPSHOT_ID = "00000000-0000-0000-0000-000000000001"
DIGEST = "a" * 64


class _DummyStateStore:
    async def record_event(self, **_: Any) -> None:
        return None


class _RaisingProvider:
    def __init__(self, exc: RuntimeSnapshotError) -> None:
        self._exc = exc
        self.called_with: dict[str, Any] = {}

    async def get_snapshot(self, *, snapshot_id: str | None, expected_digest: str | None):
        self.called_with = {
            "snapshot_id": snapshot_id,
            "expected_digest": expected_digest,
        }
        raise self._exc


async def _collect_events(stream: AsyncGenerator[Any, None]) -> list[Any]:
    items: list[Any] = []
    async for item in stream:
        items.append(item)
    return items


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (
            RuntimeSnapshotIdMissingError("missing pinned snapshot id"),
            "RUNTIME_SNAPSHOT_MISSING",
        ),
        (RuntimeSnapshotAuthError("auth rejected"), "RUNTIME_SNAPSHOT_AUTH"),
        (
            RuntimeSnapshotNotFoundError("snapshot gone"),
            "RUNTIME_SNAPSHOT_NOT_FOUND",
        ),
        (
            RuntimeSnapshotDigestMismatchError("digest mismatch"),
            "RUNTIME_SNAPSHOT_DIGEST_MISMATCH",
        ),
        (
            RuntimeSnapshotSchemaError("schema unsupported"),
            "RUNTIME_SNAPSHOT_SCHEMA",
        ),
    ],
)
def test_pipeline_stream_fails_closed_without_global_fallback(exc, code) -> None:
    request = FlowChatRequest(query="订单确认收入")
    provider = _RaisingProvider(exc)
    flow_domain = FlowDomain(
        request=request,
        flow_config_provider=provider,  # type: ignore[arg-type]
    )
    flow_domain.global_domain.state_store = _DummyStateStore()
    fallback_called = False

    async def fake_global_stream(_: Any):
        nonlocal fallback_called
        fallback_called = True
        if False:
            yield None

    def fake_prepare_runtime_request(incoming: FlowChatRequest) -> FlowChatRequest:
        return incoming

    flow_domain.global_domain._prepare_runtime_request = fake_prepare_runtime_request
    flow_domain.global_domain.pipeline_stream = fake_global_stream

    events = asyncio.run(_collect_events(flow_domain.pipeline_stream(request)))

    assert [event.event for event in events] == ["start", "error"]
    assert events[0].event == "start"
    error = events[1]
    assert error.data["finished"] is False
    assert error.data["code"] == code
    assert fallback_called is False


def test_router_injected_runtime_headers_reach_provider() -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/flow_domain/chat/v1",
        "raw_path": b"/flow_domain/chat/v1",
        "query_string": b"",
        "root_path": "",
        "headers": Headers(
            {
                "X-Runtime-Snapshot-ID": SNAPSHOT_ID,
                "X-Runtime-Snapshot-Digest": DIGEST,
            }
        ).raw,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 8000),
        "state": {},
        "app": None,
    }
    http_request = Request(scope)
    flow_domain_router._apply_runtime_headers(http_request, request_token=None)

    provider = _RaisingProvider(RuntimeSnapshotAuthError("auth rejected"))
    flow_domain = FlowDomain(
        request=FlowChatRequest(query="订单确认收入"),
        http_request=http_request,
        flow_config_provider=provider,  # type: ignore[arg-type]
    )
    flow_domain.global_domain.state_store = _DummyStateStore()
    flow_domain.global_domain._prepare_runtime_request = lambda incoming: incoming

    events = asyncio.run(
        _collect_events(flow_domain.pipeline_stream(FlowChatRequest(query="订单确认收入")))
    )

    assert provider.called_with == {
        "snapshot_id": SNAPSHOT_ID,
        "expected_digest": DIGEST,
    }
    assert [event.event for event in events] == ["start", "error"]


@pytest.mark.parametrize(
    ("snapshot_id", "digest", "expected_id", "expected_digest"),
    [
        (SNAPSHOT_ID, DIGEST, SNAPSHOT_ID, DIGEST),
        (SNAPSHOT_ID.replace("-", ""), DIGEST, SNAPSHOT_ID.replace("-", ""), DIGEST),
        ("bad id!", DIGEST, None, DIGEST),
        (SNAPSHOT_ID, "xyz", SNAPSHOT_ID, None),
        ("", "", None, None),
    ],
)
def test_apply_runtime_headers_validation(snapshot_id, digest, expected_id, expected_digest) -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/flow_domain/chat/v1",
        "raw_path": b"/flow_domain/chat/v1",
        "query_string": b"",
        "root_path": "",
        "headers": Headers(
            {
                "X-Runtime-Snapshot-ID": snapshot_id,
                "X-Runtime-Snapshot-Digest": digest,
            }
        ).raw,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 8000),
        "state": {},
        "app": None,
    }
    http_request = Request(scope)

    flow_domain_router._apply_runtime_headers(http_request, request_token=None)

    assert http_request.state.runtime_snapshot_id == expected_id
    assert http_request.state.runtime_snapshot_digest == expected_digest
