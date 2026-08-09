"""SSE frame parsing for map_core streams (R1-CONV-01 / FIX-P1-CONV-01).

map_core emits ``event: <name>\\ndata: <json>\\n\\n`` frames (one event per
frame). Buffered chunks may split frames AND UTF-8 characters arbitrarily.

:class:`SseStreamParser` keeps a single cross-chunk buffer with an
incremental UTF-8 decoder:

- bytes are decoded incrementally, so a multi-byte character split across
  chunks is never corrupted (no replacement chars);
- complete frames are extracted per feed; the incomplete tail stays in
  ``remaining`` for the next feed;
- LF and CRLF line endings, multi-line ``data:``, multiple frames per
  chunk, half frames and byte-by-byte delivery are all supported;
- invalid UTF-8 raises :class:`SseParseError` (stable error code) instead
  of silently replacing bytes.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass, field


class SseParseError(Exception):
    """Malformed stream (invalid UTF-8 / non-JSON data)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class SseFrame:
    event: str = "message"
    data: str = ""


@dataclass
class SseParseResult:
    frames: list[SseFrame] = field(default_factory=list)
    remaining: str = ""


def _extract_frames(buffer: str) -> tuple[list[SseFrame], str]:
    """Extract complete frames from ``buffer``; the tail stays as remaining.

    Frames are separated by a blank line (``\\n\\n`` or ``\\r\\n\\r\\n``).
    """
    frames: list[SseFrame] = []
    remaining = buffer
    # Normalize CRLF so "\r\n\r\n" terminates frames too.
    remaining = remaining.replace("\r\n", "\n")
    while "\n\n" in remaining:
        frame_text, remaining = remaining.split("\n\n", 1)
        event = "message"
        data_lines: list[str] = []
        for line in frame_text.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        data = "\n".join(data_lines)
        frames.append(SseFrame(event=event, data=data))
    return frames, remaining


class SseStreamParser:
    """Incremental SSE parser with a cross-chunk UTF-8 decoder."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""

    def feed(self, chunk: bytes) -> SseParseResult:
        """Decode one byte chunk and return complete frames + remaining."""
        if not chunk:
            return SseParseResult(frames=[], remaining=self._buffer)
        try:
            text = self._decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise SseParseError(
                "STREAM_DECODE_ERROR",
                f"invalid UTF-8 in stream: {exc}",
            ) from exc
        self._buffer += text
        frames, self._buffer = _extract_frames(self._buffer)
        return SseParseResult(frames=frames, remaining=self._buffer)

    def close(self) -> SseParseResult:
        """Flush decoder state at EOF; a trailing incomplete char is an error."""
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise SseParseError(
                "STREAM_DECODE_ERROR",
                f"truncated UTF-8 sequence at EOF: {exc}",
            ) from exc
        self._buffer += tail
        frames, self._buffer = _extract_frames(self._buffer)
        return SseParseResult(frames=frames, remaining=self._buffer)


def parse_sse_frames(buffer: str) -> SseParseResult:
    """Legacy one-shot parser (complete frames from a str buffer).

    Kept for compatibility with existing callers/tests; new code should use
    :class:`SseStreamParser`.
    """
    return SseParseResult(*_extract_frames(buffer))


def frame_data_json(frame: SseFrame) -> dict:
    """Parse the data field as JSON, tolerating empty payloads.

    Malformed JSON raises :class:`SseParseError` (STREAM_PARSE_ERROR) so
    callers can record a stable error instead of silently dropping data.
    """
    if not frame.data.strip():
        return {}
    try:
        value = json.loads(frame.data)
        if not isinstance(value, dict):
            raise SseParseError(
                "STREAM_PARSE_ERROR", f"expected JSON object, got {type(value).__name__}"
            )
        return value
    except json.JSONDecodeError as exc:
        raise SseParseError("STREAM_PARSE_ERROR", f"invalid JSON in frame: {exc}") from exc
