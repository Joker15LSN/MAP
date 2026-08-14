"""S2-04: IndustryChatAgent canary tests - an upstream echoing the api key
back in plain answer/message/error fields must never leak it through
content / data_source / record_message / tool-result events."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from map_core.service.agent.base import AgentRequest
from map_core.service.agent.industry_chat_agent import IndustryChatAgent

CANARY_API_KEY = "sk-fake-industry-key-0123456789abcdef012345"


class _RecordingStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record_event(self, *, state_id: str, event_type: str, payload: dict):
        self.events.append((event_type, payload))
        return None


class _FakeLLM:
    pass


def _make_agent(store: _RecordingStore) -> IndustryChatAgent:
    # instance-level assignment: module-level env may legitimately be empty
    agent = IndustryChatAgent(_FakeLLM())
    agent._api_url = "https://industry.example.com/v1/chat"
    agent._api_key = CANARY_API_KEY
    agent.set_execution_context(store, "state-1")
    return agent


def test_echoed_key_in_answer_field_is_wiped_everywhere() -> None:
    store = _RecordingStore()
    agent = _make_agent(store)

    async def fake_call(query: str) -> dict:
        return {"answer": f"the key is {CANARY_API_KEY}, bye"}

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result = asyncio.run(
            agent.run(AgentRequest(query="hi", staff_code="pytest"))
        )

    # content / data_source carry no secret
    assert CANARY_API_KEY not in result.content
    assert CANARY_API_KEY not in result.model_dump_json()
    assert "<redacted>" in result.content

    # record_message + record_tool_result events carry no secret
    for _event_type, payload in store.events:
        assert CANARY_API_KEY not in str(payload)

    message_events = [
        payload for event_type, payload in store.events if event_type == "agent_message"
    ]
    assert message_events, "record_message was not called"
    assert "<redacted>" in message_events[0]["content"]


def test_echoed_key_in_message_field_is_wiped_everywhere() -> None:
    store = _RecordingStore()
    agent = _make_agent(store)

    async def fake_call(query: str) -> dict:
        return {"message": f"invalid credential {CANARY_API_KEY}"}

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result = asyncio.run(
            agent.run(AgentRequest(query="hi", staff_code="pytest"))
        )

    assert CANARY_API_KEY not in result.content
    assert CANARY_API_KEY not in result.model_dump_json()
    for _event_type, payload in store.events:
        assert CANARY_API_KEY not in str(payload)


def test_echoed_key_in_upstream_error_is_wiped() -> None:
    store = _RecordingStore()
    agent = _make_agent(store)

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
        result = asyncio.run(
            agent.run(AgentRequest(query="hi", staff_code="pytest"))
        )

    assert result.success is False
    assert CANARY_API_KEY not in result.error
    assert CANARY_API_KEY not in result.model_dump_json()
    for _event_type, payload in store.events:
        assert CANARY_API_KEY not in str(payload)


def test_missing_config_fails_closed_without_network() -> None:
    store = _RecordingStore()
    with mock.patch.object(IndustryChatAgent, "_api_url", ""), mock.patch.object(
        IndustryChatAgent, "_api_key", ""
    ):
        agent = IndustryChatAgent(_FakeLLM())
    agent.set_execution_context(store, "state-1")

    async def fake_call(query: str) -> dict:  # must never be reached
        raise AssertionError("no network call may leave")

    with mock.patch.object(agent, "_call_industry_chat", fake_call):
        result = asyncio.run(
            agent.run(AgentRequest(query="hi", staff_code="pytest"))
        )
    assert result.success is False
    assert "CAPABILITY_CONFIG_MISSING" in result.error
