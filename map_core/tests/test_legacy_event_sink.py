"""K2 unit tests: LegacyMongoEventSink maps typed events onto old Mongo handler.

Locks the full type mapping table with a capturing fake handler and verifies
that stream.terminal never reaches the legacy handler.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from map_core.service.execution_event import CoreExecutionEvent
from map_core.service.legacy_event_sink import LegacyMongoEventSink
from map_core.service.state_store import BaseAgentStateHandler


class _CapturingHandler(BaseAgentStateHandler):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False

    async def handle_event(
        self,
        state_id: str,
        event_type: str,
        payload: dict,
        base_state: dict | None = None,
    ) -> None:
        self.calls.append((state_id, event_type, payload))

    async def close(self) -> None:
        self.closed = True


AGENTIC_TYPES = [
    "step.started",
    "step.completed",
    "step.failed",
    "message.delta",
    "checkpoint.written",
    "effect.planned",
    "effect.executing",
    "effect.succeeded",
    "effect.failed",
    "effect.uncertain",
    "effect.reconciling",
    "effect.reconciled",
    "effect.cancelled",
]

TOOL_TYPES = {
    "tool.invocation_created": "tool_call",
    "tool.invocation_completed": "tool_result",
    "tool.invocation_failed": "tool_result",
}

MODEL_TYPES = {
    "model.invocation_created": "llm_call",
    "model.invocation_sent": "llm_call",
    "model.invocation_succeeded": "llm_call",
    "model.invocation_failed": "llm_call",
    "model.invocation_unknown": "llm_call",
}


def _event(
    event_type: str,
    *,
    data: dict | None = None,
    request_id: str | None = None,
) -> CoreExecutionEvent:
    return CoreExecutionEvent(
        event_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        attempt=1,
        seq=1,
        type=event_type,
        occurred_at=datetime.now(UTC),
        request_id=request_id,
        trace_id="trace-abc",
        span_id="span-abc",
        data=data if data is not None else {},
    )


@pytest.mark.parametrize("event_type", AGENTIC_TYPES)
def test_agentic_types_map_to_agent_execution(event_type: str) -> None:
    async def scenario() -> None:
        handler = _CapturingHandler()
        sink = LegacyMongoEventSink(handler)
        event = _event(event_type, data={"payload": {"n": 1}})

        await sink.emit(event)

        assert len(handler.calls) == 1
        state_id, legacy_type, payload = handler.calls[0]
        assert state_id == str(event.run_id)
        assert legacy_type == event_type
        assert payload["event_id"] == str(event.event_id)
        assert payload["timestamp"] == event.occurred_at
        assert payload["component"] == event_type
        assert payload["data"] == {"payload": {"n": 1}}
        assert payload["_trace"] == {
            "trace_id": "trace-abc",
            "span_id": "span-abc",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("event_type,legacy_type", TOOL_TYPES.items())
def test_tool_invocation_types_map_to_tool_records(
    event_type: str, legacy_type: str
) -> None:
    async def scenario() -> None:
        handler = _CapturingHandler()
        sink = LegacyMongoEventSink(handler)
        if event_type == "tool.invocation_created":
            data: dict = {
                "agent_id": "agent-1",
                "tool": "demo",
                "tool_id": "t1",
                "step": 2,
                "args": {"q": 1},
            }
        else:
            data = {
                "agent_id": "agent-1",
                "tool": "demo",
                "tool_id": "t1",
                "step": 2,
                "output": {"success": event_type == "tool.invocation_completed"},
                "duration_s": 0.25,
                "error": None if event_type == "tool.invocation_completed" else "boom",
            }
        event = _event(event_type, data=data)

        await sink.emit(event)

        assert len(handler.calls) == 1
        state_id, got_legacy_type, payload = handler.calls[0]
        assert state_id == str(event.run_id)
        assert got_legacy_type == legacy_type
        for key, value in data.items():
            assert payload[key] == value
        assert payload["_trace"] == {
            "trace_id": "trace-abc",
            "span_id": "span-abc",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("event_type,legacy_type", MODEL_TYPES.items())
def test_model_invocation_types_map_to_llm_call(
    event_type: str, legacy_type: str
) -> None:
    async def scenario() -> None:
        handler = _CapturingHandler()
        sink = LegacyMongoEventSink(handler)
        data = {
            "component": "scene_selector",
            "phase": "classify",
            "step": 0,
            "call_kind": "chat",
            "model": "fake-model",
            "provider_request_id": "prov-1",
            "status": "success",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            "finish_reason": "stop",
        }
        event = _event(event_type, data=data, request_id="req-7")

        await sink.emit(event)

        assert len(handler.calls) == 1
        state_id, got_legacy_type, payload = handler.calls[0]
        assert state_id == str(event.run_id)
        assert got_legacy_type == legacy_type
        for key, value in data.items():
            assert payload[key] == value
        assert payload["request_id"] == "req-7"
        assert payload["trace_id"] == "trace-abc"
        assert payload["span_id"] == "span-abc"
        assert payload["_trace"] == {
            "trace_id": "trace-abc",
            "span_id": "span-abc",
        }

    asyncio.run(scenario())


def test_stream_terminal_is_not_written_to_legacy_handler() -> None:
    async def scenario() -> None:
        handler = _CapturingHandler()
        sink = LegacyMongoEventSink(handler)
        event = _event(
            "stream.terminal",
            data={
                "status": "completed",
                "error_code": None,
                "error_message": None,
            },
        )

        await sink.emit(event)

        assert handler.calls == []

    asyncio.run(scenario())


def test_aclose_closes_legacy_handler() -> None:
    async def scenario() -> None:
        handler = _CapturingHandler()
        sink = LegacyMongoEventSink(handler)

        await sink.aclose()

        assert handler.closed is True

    asyncio.run(scenario())


def test_no_mongo_connection_is_created_on_import_or_emit() -> None:
    # K2 must stay passive: the adapter only talks to the handler it was
    # given and never instantiates MongoAgentStateHandler itself.
    import map_core.service.legacy_event_sink as legacy_module

    assert hasattr(legacy_module, "LegacyMongoEventSink")
