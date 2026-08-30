"""Step 8 PR-K6: typed NDJSON core run stream adapter tests.

Uses ``httpx.MockTransport`` so the adapter's request shape (path, auth
headers, body), the full core-type mapping table, terminal projection and
every transport failure mode are pinned without a live Core process.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.runs.domain import AttemptInput, CoreError, CoreEvent, CoreOutcome, RunCommand
from app.runs.typed_core_stream import TypedCoreRunStream

ALL_CORE_EVENT_TYPES = [
    "step.started",
    "step.completed",
    "step.failed",
    "message.delta",
    "tool.invocation_created",
    "tool.invocation_completed",
    "tool.invocation_failed",
    "model.invocation_created",
    "model.invocation_sent",
    "model.invocation_succeeded",
    "model.invocation_failed",
    "model.invocation_unknown",
    "checkpoint.written",
    "effect.planned",
    "effect.executing",
    "effect.succeeded",
    "effect.failed",
    "effect.uncertain",
    "effect.reconciling",
    "effect.reconciled",
    "effect.cancelled",
]


def _attempt() -> AttemptInput:
    return AttemptInput(
        run_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        attempt=1,
        command=RunCommand(
            kind="conversation_turn",
            payload={"query": "hello"},
            snapshot={"runtime": "v1"},
        ),
    )


def _ndjson_line(
    *,
    event_type: str,
    seq: int,
    data: dict | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "attempt": 1,
            "seq": seq,
            "type": event_type,
            "occurred_at": "2026-08-15T12:00:00Z",
            "data": data or {},
        }
    )


def _terminal_line(status: str, **data) -> str:
    base = {"status": status, "error_code": None, "error_message": None}
    base.update(data)
    return _ndjson_line(event_type="stream.terminal", seq=999, data=base)


def _client(
    handler,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(stream: TypedCoreRunStream, attempt: AttemptInput) -> list:
    return [item async for item in stream.stream(attempt)]


async def test_request_shape_and_full_type_mapping() -> None:
    captured: dict = {}
    attempt = _attempt()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        body_lines = [
            _ndjson_line(event_type=event_type, seq=idx)
            for idx, event_type in enumerate(ALL_CORE_EVENT_TYPES, start=1)
        ]
        body_lines.append(_terminal_line("completed"))
        return httpx.Response(
            200,
            content=("\n".join(body_lines) + "\n").encode(),
            headers={"content-type": "application/x-ndjson"},
        )

    async with _client(handler) as client:
        stream = TypedCoreRunStream(
            client,
            core_origin="http://core.test",
            token="run-token",
            audience="map-core",
        )
        items = await _collect(stream, attempt)

    assert captured["method"] == "POST"
    assert captured["path"] == (
        f"/internal/v1/runs/{attempt.run_id}/attempts/{attempt.attempt}/events"
    )
    assert captured["headers"]["authorization"] == "Bearer run-token"
    assert captured["headers"]["x-service-name"] == "map-bff"
    assert captured["headers"]["x-service-audience"] == "map-core"
    assert captured["headers"]["x-workspace-id"] == str(attempt.workspace_id)
    assert captured["headers"]["x-request-id"] == str(attempt.run_id)
    assert captured["body"] == {"query": "hello"}

    assert items == [
        CoreEvent(type=event_type, data={}) for event_type in ALL_CORE_EVENT_TYPES
    ] + [CoreOutcome(status="completed")]


async def test_terminal_failed_projects_core_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = (
            _ndjson_line(event_type="step.started", seq=1)
            + "\n"
            + _terminal_line(
                "failed",
                error_code="CORE_BAD",
                error_message="core exploded",
            )
            + "\n"
        )
        return httpx.Response(200, content=content.encode())

    async with _client(handler) as client:
        stream = TypedCoreRunStream(
            client, core_origin="http://core.test", token="t"
        )
        items = await _collect(stream, _attempt())
    assert items[-1] == CoreOutcome(
        status="failed", error_code="CORE_BAD", error_message="core exploded"
    )


@pytest.mark.parametrize("status_code", [401, 403, 404, 500])
async def test_http_error_projects_core_error(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "nope"})

    async with _client(handler) as client:
        stream = TypedCoreRunStream(
            client, core_origin="http://core.test", token="t"
        )
        items = await _collect(stream, _attempt())
    assert len(items) == 1
    assert items[0] == CoreError(
        code=f"STREAM_CORE_HTTP_{status_code}",
        message=f"core typed stream HTTP {status_code}",
    )


async def test_unknown_type_projects_core_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = (
            _ndjson_line(event_type="bogus.type", seq=1) + "\n"
            + _terminal_line("completed") + "\n"
        )
        return httpx.Response(200, content=content.encode())

    async with _client(handler) as client:
        stream = TypedCoreRunStream(
            client, core_origin="http://core.test", token="t"
        )
        items = await _collect(stream, _attempt())
    assert len(items) == 1
    assert items[0] == CoreError(
        code="STREAM_CORE_ERROR",
        message="unknown core event type 'bogus.type'",
    )


async def test_malformed_json_line_projects_core_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json}\n")

    async with _client(handler) as client:
        stream = TypedCoreRunStream(
            client, core_origin="http://core.test", token="t"
        )
        items = await _collect(stream, _attempt())
    assert len(items) == 1
    assert isinstance(items[0], CoreError)
    assert "malformed core event JSON" in items[0].message


async def test_eof_without_terminal_projects_core_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(_ndjson_line(event_type="step.started", seq=1) + "\n").encode(),
        )

    async with _client(handler) as client:
        stream = TypedCoreRunStream(
            client, core_origin="http://core.test", token="t"
        )
        items = await _collect(stream, _attempt())
    assert items[0] == CoreEvent(type="step.started", data={})
    assert items[-1] == CoreError(
        code="STREAM_CORE_ERROR",
        message="stream ended without stream.terminal",
    )
