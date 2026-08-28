"""BFF -> core remote-owned seam for Canonical Run attempts.

The port is :class:`CoreRunStream`. Two adapters exist from day one:

- :class:`HttpCoreRunStream` - production transport. It owns ALL knowledge
  of the legacy core SSE wire shape (endpoint path, headers, SSE parsing,
  legacy ``start/content_delta/meta/done/error`` frames) and projects it to
  the typed :class:`CoreItem` stream. Nothing else in the BFF may parse core
  SSE frames for a Run attempt.
- :class:`InMemoryCoreRunStream` - deterministic scripted adapter used by
  contract tests; it replays the SAME typed items.

core never writes Run/Event PG (ADR-0002): it only produces this stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Protocol

from ..core_client import MapCoreClient
from ..services.sse import SseFrame, SseParseError, SseStreamParser, frame_data_json
from .domain import AttemptInput, CoreEvent, CoreItem, CoreOutcome

_CORE_GLOBAL_STREAM_PATH = "/global_domain/chat/stream/v2"
_LEGACY_STREAM_ERROR = "STREAM_CORE_ERROR"


class CoreRunStream(Protocol):
    def stream(self, attempt: AttemptInput) -> AsyncIterator[CoreItem]: ...


class HttpCoreRunStream:
    """Production adapter: legacy core SSE -> typed CoreItem stream."""

    def __init__(self, client: MapCoreClient) -> None:
        self._client = client

    async def stream(self, attempt: AttemptInput) -> AsyncIterator[CoreItem]:
        headers = {
            "X-Request-ID": str(attempt.run_id),
            "X-Workspace-ID": str(attempt.workspace_id),
        }
        parser = SseStreamParser()
        accumulated_content: list[str] = []
        terminal_seen = False

        async def _emit(raw_chunk: bytes | None) -> AsyncIterator[CoreItem]:
            nonlocal terminal_seen
            result = parser.feed(raw_chunk) if raw_chunk is not None else parser.close()
            for frame in result.frames:
                if terminal_seen:
                    continue
                item = _project_legacy_frame(
                    frame,
                    data=frame_data_json(frame),
                    accumulated_content=accumulated_content,
                )
                if item is None:
                    continue
                if isinstance(item, CoreOutcome):
                    terminal_seen = True
                    yield item
                    continue
                yield item
                if frame.event == "done":
                    terminal_seen = True
                    yield CoreOutcome(status="completed")

        async for chunk in self._client.stream_chat_by_path(
            _CORE_GLOBAL_STREAM_PATH,
            dict(attempt.command.payload),
            headers,
        ):
            async for item in _emit(chunk):
                yield item
        async for item in _emit(None):
            yield item
        if not terminal_seen:
            raise SseParseError(
                "STREAM_EOF_WITHOUT_DONE", "stream ended without done/error"
            )


class InMemoryCoreRunStream:
    """Deterministic adapter: replays a scripted typed item sequence."""

    def __init__(self, script: Iterable[CoreItem]) -> None:
        self._script = list(script)

    async def stream(self, attempt: AttemptInput) -> AsyncIterator[CoreItem]:
        for item in self._script:
            yield item


def _project_legacy_frame(
    frame: SseFrame, *, data: dict, accumulated_content: list[str]
) -> CoreItem | None:
    """Project ONE legacy core SSE frame into the typed contract.

    ``content_delta`` projects to ``message.delta`` (the frozen canonical
    fact for an assistant-message content increment) AND is buffered so the
    final ``done`` can still emit one authoritative ``step.completed`` full
    text; ``meta`` maps to ``checkpoint.written`` (a content checkpoint is
    the closest frozen fact and keeps replayable state inside the event
    stream).
    """
    if frame.event not in {"start", "content_delta", "meta", "done", "error"}:
        return None
    if frame.event == "start":
        return CoreEvent(type="step.started", data=dict(data))
    if frame.event == "content_delta":
        delta = data.get("delta")
        if isinstance(delta, str):
            accumulated_content.append(delta)
            return CoreEvent(type="message.delta", data={"content": delta})
        return None
    if frame.event == "meta":
        return CoreEvent(type="checkpoint.written", data={"meta": data})
    if frame.event == "done":
        return CoreEvent(
            type="step.completed",
            data={"content": "".join(accumulated_content), **data},
        )
    if frame.event == "error":
        return CoreOutcome(
            status="failed",
            error_code=str(data.get("code") or _LEGACY_STREAM_ERROR),
            error_message=str(data.get("message") or data.get("error") or "core error"),
        )
    return None
