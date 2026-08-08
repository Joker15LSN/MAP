"""Minimal SSE frame parsing for map_core streams (R1-CONV-01).

map_core emits ``event: <name>\\ndata: <json>\\n\\n`` frames (one event per
frame). Buffered chunks may split frames arbitrarily; we accumulate until
a blank line and parse the ``event``/``data`` fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class SseFrame:
    event: str = "message"
    data: str = ""


@dataclass
class SseParseResult:
    frames: list[SseFrame] = field(default_factory=list)
    remaining: str = ""


def parse_sse_frames(buffer: str) -> SseParseResult:
    """Parse complete frames from ``buffer``; incomplete tail stays in remaining."""
    frames: list[SseFrame] = []
    remaining = buffer
    while "\n\n" in remaining:
        frame_text, remaining = remaining.split("\n\n", 1)
        event = "message"
        data_lines: list[str] = []
        for line in frame_text.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        data = "\n".join(data_lines)
        frames.append(SseFrame(event=event, data=data))
    return SseParseResult(frames=frames, remaining=remaining)


def frame_data_json(frame: SseFrame) -> dict:
    """Parse the data field as JSON, tolerating empty/malformed payloads."""
    if not frame.data.strip():
        return {}
    try:
        value = json.loads(frame.data)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}
