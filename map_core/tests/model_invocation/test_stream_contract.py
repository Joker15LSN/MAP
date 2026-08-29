"""Contract tests for streaming ModelInvocation.invoke."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from map_core.config.config_schema import LLMConfig
from map_core.utils.model_invocation import (
    ModelInvocation,
    ModelInvocationRequest,
    ProviderError,
    ProviderStream,
)
from tests.model_invocation.scripted_provider import (
    ScriptedProvider,
    content_chunk,
    reasoning_chunk,
    run_async,
    stream_of,
    usage_chunk,
)


def _config(**overrides) -> LLMConfig:
    base = {
        "base_url": "http://llm.test/v1",
        "api_key": "k",
        "model": "test-model",
        "max_retries": 1,
    }
    base.update(overrides)
    return LLMConfig(**base)


def _request(**overrides) -> ModelInvocationRequest:
    base = {"messages": [{"role": "user", "content": "q"}], "stream": True}
    base.update(overrides)
    return ModelInvocationRequest(**base)


async def _events(invocation: ModelInvocation, request: ModelInvocationRequest):
    stream = await invocation.invoke(request)
    return [event async for event in stream]


@run_async
async def test_stream_event_order_content_reasoning_usage_terminal_succeeded() -> None:
    provider = ScriptedProvider(
        [
            stream_of(
                [
                    content_chunk("hi"),
                    reasoning_chunk("think"),
                    usage_chunk(),
                ],
                complete=True,
            )
        ]
    )
    invocation = ModelInvocation(_config(), provider=provider)

    events = await _events(invocation, _request())

    assert [event.type for event in events] == [
        "content",
        "reasoning",
        "usage",
        "terminal",
    ]
    terminal = events[-1]
    assert terminal.status == "succeeded"
    assert terminal.usage is not None
    assert terminal.usage.to_dict() == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    content_event = events[0]
    assert content_event.data is not None
    assert content_event.data["type"] == "content"
    assert content_event.data["data"] == "hi"
    assert content_event.data["raw_chunk"] == content_chunk("hi")
    assert events[2].data == {
        "type": "usage",
        "data": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


@run_async
async def test_stream_mid_cancel_yields_terminal_cancelled() -> None:
    cancel = asyncio.Event()

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        yield content_chunk("before-cancel")
        cancel.set()
        yield content_chunk("after-cancel")

    provider = ScriptedProvider([ProviderStream(chunks(), complete=True)])
    invocation = ModelInvocation(_config(), provider=provider)

    events = await _events(invocation, _request(cancel=cancel))

    assert [event.type for event in events] == ["content", "terminal"]
    assert events[0].data is not None
    assert events[0].data["data"] == "before-cancel"
    assert events[-1].status == "cancelled"
    assert events[-1].error is not None
    assert events[-1].error.code == "cancelled"


@run_async
async def test_stream_clean_but_incomplete_yields_terminal_unknown() -> None:
    provider = ScriptedProvider(
        [stream_of([content_chunk("partial")], complete=False)]
    )
    invocation = ModelInvocation(_config(), provider=provider)

    events = await _events(invocation, _request())

    assert [event.type for event in events] == ["content", "terminal"]
    terminal = events[-1]
    assert terminal.status == "unknown"
    assert terminal.error is not None
    assert terminal.error.code == "unknown"


@run_async
async def test_stream_error_retryable_retries_whole_stream() -> None:
    async def broken_chunks() -> AsyncIterator[dict[str, Any]]:
        yield content_chunk("partial-from-first")
        raise ProviderError("timeout", "mid-stream timeout", True)

    provider = ScriptedProvider(
        [
            ProviderStream(broken_chunks(), complete=False),
            stream_of([content_chunk("full-from-second")], complete=True),
        ]
    )
    invocation = ModelInvocation(_config(max_retries=1), provider=provider)

    events = await _events(invocation, _request())

    assert [event.type for event in events] == [
        "content",
        "content",
        "terminal",
    ]
    assert events[0].data is not None and events[0].data["data"] == "partial-from-first"
    assert events[1].data is not None and events[1].data["data"] == "full-from-second"
    assert events[-1].status == "succeeded"
    assert events[-1].data is not None
    assert events[-1].data["attempts"] == 2


@run_async
async def test_stream_error_non_retryable_yields_terminal_failed() -> None:
    async def broken_chunks() -> AsyncIterator[dict[str, Any]]:
        yield content_chunk("partial")
        raise ProviderError("provider_error", "bad stream", False, status=400)

    provider = ScriptedProvider(
        [ProviderStream(broken_chunks(), complete=False)]
    )
    invocation = ModelInvocation(_config(max_retries=3), provider=provider)

    events = await _events(invocation, _request())

    assert [event.type for event in events] == ["content", "terminal"]
    assert events[-1].status == "failed"
    assert events[-1].error is not None
    assert events[-1].error.code == "provider_error"
    assert events[-1].error.retryable is False
    assert events[-1].data is not None
    assert events[-1].data["attempts"] == 1


@run_async
async def test_stream_request_creation_error_retryable_retries() -> None:
    provider = ScriptedProvider(
        [
            ProviderError("timeout", "connect timeout", True),
            stream_of([content_chunk("ok")], complete=True),
        ]
    )
    invocation = ModelInvocation(_config(max_retries=1), provider=provider)

    events = await _events(invocation, _request())

    assert [event.type for event in events] == ["content", "terminal"]
    assert events[-1].status == "succeeded"
    assert events[-1].data is not None
    assert events[-1].data["attempts"] == 2
