"""Parity tests: LLMEngine.ainvoke translates to ModelInvocation.invoke."""

from __future__ import annotations

from map_core.config.config_schema import LLMConfig
from map_core.utils.llm_engine import LLMEngine
from map_core.utils.model_invocation import ModelInvocation, ProviderResponse
from tests.model_invocation.scripted_provider import (
    ScriptedProvider,
    completion_payload,
    run_async,
)


def _config() -> LLMConfig:
    return LLMConfig(
        base_url="http://llm.test/v1",
        api_key="k",
        model="test-model",
        max_retries=0,
    )


@run_async
async def test_ainvoke_matches_model_invocation_semantics() -> None:
    payload = completion_payload(content="parity")
    shell_provider = ScriptedProvider([ProviderResponse(payload=payload)])
    shell = LLMEngine(_config())
    shell._invocation = ModelInvocation(_config(), provider=shell_provider)

    response = await shell.ainvoke([{"role": "user", "content": "q"}])

    assert response.content == "parity"
    assert response.usage == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    assert response.model == "test-model"
    assert response.finish_reason == "stop"
    assert response.request_id == "req-1"
    assert isinstance(response.raw, dict)


@run_async
async def test_ask_tool_maps_neutral_tool_calls() -> None:
    payload = completion_payload(
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
    )
    provider = ScriptedProvider([ProviderResponse(payload=payload)])
    shell = LLMEngine(_config())
    shell._invocation = ModelInvocation(_config(), provider=provider)

    response = await shell.ask_tool(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
    )

    assert response.content == ""
    assert response.tool_calls is not None
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].function.name == "search"
    assert response.finish_reason == "tool_calls"


@run_async
async def test_ask_tool_empty_choices_stays_empty_success() -> None:
    payload = completion_payload(content="", choices=[])
    provider = ScriptedProvider([ProviderResponse(payload=payload)])
    shell = LLMEngine(_config())
    shell._invocation = ModelInvocation(_config(), provider=provider)

    response = await shell.ask_tool(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "search"}}],
    )

    assert response.content == ""
    assert response.tool_calls == []
