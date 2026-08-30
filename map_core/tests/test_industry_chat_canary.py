"""S2-04: IndustryChatAgent canary tests.

Upstream api-key echoes must never leak into content/data_source/events."""

import asyncio
from unittest import mock

from map_core.service.agent.base import AgentRequest
from map_core.service.agent.industry_chat_agent import IndustryChatAgent
from map_core.service.execution_event import set_run_context

# Security-scan exemption target: this canary lives at a stable line number.
CANARY_API_KEY = "sk-fake-industry-key-0123456789abcdef012345"


class _FakeLLM:
    pass


def _make_agent() -> IndustryChatAgent:
    # instance-level assignment: module-level env may legitimately be empty
    agent = IndustryChatAgent(_FakeLLM())
    agent._api_url = "https://industry.example.com/v1/chat"
    agent._api_key = CANARY_API_KEY
    return agent


def _run_agent_with_recording(agent: IndustryChatAgent):
    from tests.run_context_utils import make_run_context_sink

    run_context, sink = make_run_context_sink()

    async def _run():
        with set_run_context(run_id=run_context.run_id):
            return await agent.run(AgentRequest(query="hi", staff_code="pytest"))

    return asyncio.run(_run()), sink


def test_echoed_key_in_answer_field_is_wiped_everywhere() -> None:
    agent = _make_agent()

    async def fake_call(query: str) -> dict:
        return {"answer": f"the key is {CANARY_API_KEY}, bye"}

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result, sink = _run_agent_with_recording(agent)

    # content / data_source carry no secret
    assert CANARY_API_KEY not in result.content
    assert CANARY_API_KEY not in result.model_dump_json()
    assert "<redacted>" in result.content

    # record_message + record_tool_result events carry no secret
    for event in sink.events:
        assert CANARY_API_KEY not in str(event.data)

    message_events = [
        event.data
        for event in sink.events
        if event.type == "message.delta" and event.data.get("component") == "agent_message"
    ]
    assert message_events, "record_message was not called"
    assert "<redacted>" in message_events[0]["content"]


def test_echoed_key_in_message_field_is_wiped_everywhere() -> None:
    agent = _make_agent()

    async def fake_call(query: str) -> dict:
        return {"message": f"invalid credential {CANARY_API_KEY}"}

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result, sink = _run_agent_with_recording(agent)

    assert CANARY_API_KEY not in result.content
    assert CANARY_API_KEY not in result.model_dump_json()
    for event in sink.events:
        assert CANARY_API_KEY not in str(event.data)


def test_echoed_key_in_upstream_error_is_wiped() -> None:
    agent = _make_agent()

    import httpx

    class Boom(Exception):
        pass

    async def fake_call(query: str) -> dict:
        raise httpx.HTTPStatusError(
            "upstream rejected key " + CANARY_API_KEY,
            request=httpx.Request("POST", "https://industry.example.com/v1/chat"),
            response=httpx.Response(
                401, text=f'{{"error": "bad key {CANARY_API_KEY}"}}'
            ),
        )

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result, sink = _run_agent_with_recording(agent)

    assert result.success is False
    assert CANARY_API_KEY not in result.error
    assert CANARY_API_KEY not in result.model_dump_json()
    for event in sink.events:
        assert CANARY_API_KEY not in str(event.data)


def test_missing_config_fails_closed_without_network() -> None:
    with mock.patch.object(IndustryChatAgent, "_api_url", ""), mock.patch.object(
        IndustryChatAgent, "_api_key", ""
    ):
        agent = IndustryChatAgent(_FakeLLM())

    async def fake_call(query: str) -> dict:  # must never be reached
        raise AssertionError("no network call may leave")

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result, _sink = _run_agent_with_recording(agent)
    assert result.success is False
    assert "CAPABILITY_CONFIG_MISSING" in result.error
