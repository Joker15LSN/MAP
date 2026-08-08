from __future__ import annotations

from typing import Any

from agentscope.event import ModelCallEndEvent, ReplyEndEvent


def model_usage(event: ModelCallEndEvent) -> dict[str, int]:
    return {
        "prompt_tokens": event.input_tokens,
        "completion_tokens": event.output_tokens,
    }


def reply_end_reason(event: ReplyEndEvent) -> str:
    reason: Any = event.finished_reason
    return str(getattr(reason, "value", reason))
