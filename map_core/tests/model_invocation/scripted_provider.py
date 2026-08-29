"""Shared scripted provider helpers for ModelInvocation contract tests."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from typing import Any, Callable, Coroutine, TypeVar

T = TypeVar("T")


def run_async(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., T]:
    """Adapt an async test to the repo's sync pytest setup (no pytest-asyncio)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return asyncio.run(func(*args, **kwargs))

    return wrapper

from map_core.utils.model_invocation import (
    ModelInvocationOutcome,
    ModelUsage,
    PreparedRequest,
    ProviderError,
    ProviderResponse,
    ProviderStream,
)


def tool_outcome(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    model: str | None = None,
    usage: dict[str, int] | None = None,
    finish_reason: str | None = None,
    response_time: float = 0.0,
    request_id: str | None = None,
) -> ModelInvocationOutcome:
    """Build a succeeded ``ModelInvocationOutcome`` from legacy response kwargs."""
    mapped_tool_calls: list[dict[str, Any]] | None = None
    if tool_calls:
        mapped_tool_calls = [call.model_dump() for call in tool_calls]
    mapped_usage: ModelUsage | None = None
    if usage:
        mapped_usage = ModelUsage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
    return ModelInvocationOutcome(
        status="succeeded",
        content=content,
        tool_calls=mapped_tool_calls,
        usage=mapped_usage,
        finish_reason=finish_reason,
        model=model,
        request_id=request_id,
        attempts=1,
        latency_ms=response_time * 1000.0,
    )


class ScriptedProvider:
    """Plays back a script of ProviderResponse / ProviderStream / ProviderError."""

    def __init__(
        self, script: list[ProviderResponse | ProviderStream | ProviderError]
    ) -> None:
        self.script = list(script)
        self.calls: list[PreparedRequest] = []

    async def request(
        self, prepared: PreparedRequest
    ) -> ProviderResponse | ProviderStream:
        self.calls.append(prepared)
        if not self.script:
            raise ProviderError("provider_error", "script exhausted", False)
        item = self.script.pop(0)
        if isinstance(item, ProviderError):
            raise item
        return item


def completion_payload(
    *,
    content: str = "hello",
    model: str = "test-model",
    request_id: str = "req-1",
    usage: dict[str, int] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": choices
        if choices is not None
        else [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


def content_chunk(
    content: str = "hi",
    *,
    model: str = "test-model",
    chunk_id: str = "chunk-1",
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def reasoning_chunk(reasoning: str = "thinking") -> dict[str, Any]:
    return {
        "id": "chunk-r",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "delta": {"reasoning_content": reasoning},
                "finish_reason": None,
            }
        ],
    }


def usage_chunk(
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": "chunk-usage",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [],
        "usage": usage or {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


def stream_of(
    chunks: list[dict[str, Any]],
    *,
    complete: bool = True,
) -> ProviderStream:
    async def _gen() -> AsyncIterator[dict[str, Any]]:
        for chunk in chunks:
            yield chunk

    return ProviderStream(_gen(), complete=complete)
