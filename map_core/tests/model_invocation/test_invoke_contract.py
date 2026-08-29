"""Contract tests for non-stream ModelInvocation.invoke."""

from __future__ import annotations

import asyncio

from map_core.config.config_schema import LLMConfig
from map_core.utils.model_invocation import (
    ModelInvocation,
    ModelInvocationRequest,
    ProviderError,
    ProviderResponse,
    StructuredOutput,
)
from map_core.utils.model_invocation.engine import JSON_SCHEMA_UNAVAILABLE_MESSAGE
from tests.model_invocation.scripted_provider import (
    ScriptedProvider,
    completion_payload,
    run_async,
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
    base = {"messages": [{"role": "user", "content": "q"}]}
    base.update(overrides)
    return ModelInvocationRequest(**base)


def _completion(*, content: str = "hello", **overrides) -> dict:
    return completion_payload(content=content, **overrides)


@run_async
async def test_success_content_usage_model_request_id_attempts_latency_raw() -> None:
    payload = _completion()
    provider = ScriptedProvider([ProviderResponse(payload=payload)])
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(_request())

    assert outcome.status == "succeeded"
    assert outcome.content == "hello"
    assert outcome.usage is not None
    assert outcome.usage.to_dict() == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    assert outcome.model == "test-model"
    assert outcome.request_id == "req-1"
    assert outcome.attempts == 1
    assert outcome.latency_ms >= 0
    assert isinstance(outcome.raw, dict)
    assert outcome.raw["id"] == "req-1"
    assert outcome.error is None


@run_async
async def test_raw_compat_false_has_no_raw() -> None:
    provider = ScriptedProvider([ProviderResponse(payload=_completion())])
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(_request(raw_compat=False))

    assert outcome.status == "succeeded"
    assert outcome.raw is None


@run_async
async def test_429_retry_then_success_attempts_gt_1() -> None:
    provider = ScriptedProvider(
        [
            ProviderError("rate_limited", "rate limited", True, status=429),
            ProviderResponse(payload=_completion(content="ok")),
        ]
    )
    invocation = ModelInvocation(_config(max_retries=1), provider=provider)

    outcome = await invocation.invoke(_request())

    assert outcome.status == "succeeded"
    assert outcome.content == "ok"
    assert outcome.attempts == 2
    assert len(provider.calls) == 2


@run_async
async def test_timeout_exhausted_failed_timeout_attempts_max_retries_plus_1() -> None:
    provider = ScriptedProvider(
        [
            ProviderError("timeout", "timeout", True),
            ProviderError("timeout", "timeout", True),
        ]
    )
    invocation = ModelInvocation(_config(max_retries=1), provider=provider)

    outcome = await invocation.invoke(_request())

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error.code == "timeout"
    assert outcome.attempts == 2
    assert len(provider.calls) == 2


@run_async
async def test_4xx_provider_error_no_retry() -> None:
    provider = ScriptedProvider(
        [ProviderError("provider_error", "bad request", False, status=400)]
    )
    invocation = ModelInvocation(_config(max_retries=3), provider=provider)

    outcome = await invocation.invoke(_request())

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error.code == "provider_error"
    assert outcome.error.retryable is False
    assert outcome.attempts == 1
    assert len(provider.calls) == 1


@run_async
async def test_invalid_request_empty_messages_does_not_call_provider() -> None:
    provider = ScriptedProvider([])
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(_request(messages=[]))

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error.code == "invalid_request"
    assert outcome.attempts == 0
    assert provider.calls == []


@run_async
async def test_invalid_request_structured_and_tools_does_not_call_provider() -> None:
    provider = ScriptedProvider([])
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(
        _request(
            structured=StructuredOutput(schema={"type": "object"}),
            tools=[{"type": "function", "function": {"name": "search"}}],
        )
    )

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error.code == "invalid_request"
    assert outcome.attempts == 0
    assert provider.calls == []


@run_async
async def test_structured_json_schema_success() -> None:
    provider = ScriptedProvider(
        [ProviderResponse(payload=_completion(content='{"a": 1}'))]
    )
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(
        _request(structured=StructuredOutput(schema={"type": "object"}))
    )

    assert outcome.status == "succeeded"
    assert outcome.structured == {"a": 1}
    assert outcome.content == '{"a": 1}'
    assert provider.calls[0].params["response_format"]["type"] == "json_schema"


@run_async
async def test_structured_json_schema_unavailable_falls_back_to_json_object() -> None:
    unavailable = ProviderError(
        "provider_error",
        "unavailable",
        False,
        status=400,
        body={"error": {"message": JSON_SCHEMA_UNAVAILABLE_MESSAGE}},
    )
    provider = ScriptedProvider(
        [
            unavailable,
            ProviderResponse(payload=_completion(content='{"b": 2}')),
        ]
    )
    invocation = ModelInvocation(_config(max_retries=0), provider=provider)

    outcome = await invocation.invoke(
        _request(structured=StructuredOutput(schema={"type": "object"}))
    )

    assert outcome.status == "succeeded"
    assert outcome.structured == {"b": 2}
    assert len(provider.calls) == 2
    assert provider.calls[1].params["response_format"] == {"type": "json_object"}
    assert any(
        "Use JSON format as output." in str(msg.get("content", ""))
        for msg in provider.calls[1].messages
    )


@run_async
async def test_structured_invalid_json_is_refused_without_retry() -> None:
    provider = ScriptedProvider(
        [ProviderResponse(payload=_completion(content="not-json"))]
    )
    invocation = ModelInvocation(_config(max_retries=3), provider=provider)

    outcome = await invocation.invoke(
        _request(structured=StructuredOutput(schema={"type": "object"}))
    )

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error.code == "structured_refused"
    assert outcome.attempts == 1
    assert len(provider.calls) == 1


@run_async
async def test_tools_tool_choice_success_neutral_tool_call_dicts() -> None:
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q": "x"}'},
        }
    ]
    provider = ScriptedProvider(
        [
            ProviderResponse(
                payload=_completion(content="", tool_calls=tool_calls, finish_reason="tool_calls")
            )
        ]
    )
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(
        _request(
            tools=[{"type": "function", "function": {"name": "search"}}],
            tool_choice="required",
        )
    )

    assert outcome.status == "succeeded"
    assert outcome.content == ""
    assert outcome.tool_calls == tool_calls
    assert outcome.finish_reason == "tool_calls"
    assert provider.calls[0].params["tools"][0]["function"]["name"] == "search"
    assert provider.calls[0].params["tool_choice"] == "required"


@run_async
async def test_structured_parse_false_preserves_legacy_tolerant_content() -> None:
    provider = ScriptedProvider(
        [ProviderResponse(payload=_completion(content="```json\nnot-json\n```"))]
    )
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(
        _request(structured=StructuredOutput(schema={"type": "object"}, parse=False))
    )

    assert outcome.status == "succeeded"
    assert outcome.structured is None
    assert outcome.content == "```json\nnot-json\n```"


@run_async
async def test_tools_empty_choices_is_empty_success_legacy_parity() -> None:
    provider = ScriptedProvider(
        [ProviderResponse(payload=_completion(content="", choices=[]))]
    )
    invocation = ModelInvocation(_config(), provider=provider)

    outcome = await invocation.invoke(
        _request(tools=[{"type": "function", "function": {"name": "search"}}])
    )

    assert outcome.status == "succeeded"
    assert outcome.content == ""
    assert outcome.tool_calls == []
    assert outcome.error is None


@run_async
async def test_cancel_before_network_returns_cancelled_attempts_0() -> None:
    provider = ScriptedProvider([])
    invocation = ModelInvocation(_config(), provider=provider)
    cancel = asyncio.Event()
    cancel.set()

    outcome = await invocation.invoke(_request(cancel=cancel))

    assert outcome.status == "cancelled"
    assert outcome.attempts == 0
    assert outcome.error is not None
    assert outcome.error.code == "cancelled"
    assert provider.calls == []
