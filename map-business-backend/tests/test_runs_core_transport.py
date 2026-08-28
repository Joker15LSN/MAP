"""CoreRunStream seam contract tests (Step 2 / PR-C).

Both adapters cross the same typed seam: the in-memory adapter replays
scripted items; the HTTP adapter projects the legacy core SSE wire shape
without letting it leak past the adapter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest

from app.runs.core_transport import HttpCoreRunStream, InMemoryCoreRunStream
from app.runs.domain import AttemptInput, CoreEvent, CoreOutcome, RunCommand


class _FakeCoreClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.path: str | None = None
        self.headers: dict[str, str] | None = None

    async def stream_chat_by_path(
        self, path: str, payload: dict, headers: dict
    ) -> AsyncGenerator[bytes, None]:
        self.path = path
        self.payload = payload
        self.headers = headers
        for chunk in self._chunks:
            yield chunk


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


async def test_in_memory_adapter_replays_typed_script_in_order() -> None:
    script = [
        CoreEvent(type="step.started", data={"step_id": "s-1"}),
        CoreOutcome(status="completed"),
    ]
    stream = InMemoryCoreRunStream(script)
    items = [item async for item in stream.stream(_attempt())]
    assert items == script


async def test_http_adapter_projects_legacy_frames_and_buffers_deltas() -> None:
    client = _FakeCoreClient(
        [
            b'event: start\ndata: {"context": "c"}\n\n',
            b'event: content_delta\ndata: {"delta": "hel"}\n\n',
            b'event: content_delta\ndata: {"delta": "lo"}\n\n',
            b'event: done\ndata: {"result": {}}\n\n',
        ]
    )
    stream = HttpCoreRunStream(client)
    items = [item async for item in stream.stream(_attempt())]
    assert items == [
        CoreEvent(type="step.started", data={"context": "c"}),
        CoreEvent(type="message.delta", data={"content": "hel"}),
        CoreEvent(type="message.delta", data={"content": "lo"}),
        CoreEvent(
            type="step.completed",
            data={"content": "hello", "result": {}},
        ),
        CoreOutcome(status="completed"),
    ]
    assert client.path == "/global_domain/chat/stream/v2"


async def test_http_adapter_projects_legacy_error_as_failed_outcome() -> None:
    client = _FakeCoreClient(
        [
            b'event: start\ndata: {}\n\n',
            b'event: error\ndata: {"code": "CORE_BAD", "message": "bad"}\n\n',
        ]
    )
    items = [item async for item in HttpCoreRunStream(client).stream(_attempt())]
    assert items[-1] == CoreOutcome(
        status="failed", error_code="CORE_BAD", error_message="bad"
    )


async def test_http_adapter_eof_without_done_fails() -> None:
    client = _FakeCoreClient([b'event: start\ndata: {}\n\n'])
    with pytest.raises(Exception, match="stream ended without done"):
        _ = [item async for item in HttpCoreRunStream(client).stream(_attempt())]
