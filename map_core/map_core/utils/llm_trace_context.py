from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo

_LLM_TRACE_CONTEXT: ContextVar[dict[str, Any]] = ContextVar(
    "map_llm_trace_context",
    default={},
)


def get_llm_trace_context() -> dict[str, Any]:
    return dict(_LLM_TRACE_CONTEXT.get() or {})


@contextmanager
def llm_trace_context(**updates: Any) -> Iterator[None]:
    current = get_llm_trace_context()
    merged = {**current, **{k: v for k, v in updates.items() if v is not None}}
    token = _LLM_TRACE_CONTEXT.set(merged)
    try:
        yield
    finally:
        _LLM_TRACE_CONTEXT.reset(token)


def now_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def summarize_llm_messages(messages: Any, *, max_chars: int = 480) -> str:
    if not isinstance(messages, list):
        return ""

    parts: list[str] = []
    for item in messages[:6]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "unknown")
        content = item.get("content")
        if content is None:
            content = ""
        text = str(content).replace("\n", " ").strip()
        if len(text) > 80:
            text = f"{text[:80]}..."
        parts.append(f"{role}:{text}")

    summary = " | ".join(parts)
    return summary if len(summary) <= max_chars else f"{summary[:max_chars]}..."
