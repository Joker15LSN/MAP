"""Step 8 PR-K2: legacy Mongo event sink adapter.

Projects ``CoreExecutionEvent`` back onto the existing
``MongoAgentStateHandler.handle_event(state_id, event_type, payload)``
contract so K3/K4 can replace ``GlobalAgentStateStore`` callers without
losing the old MongoDB collections.  This module is intentionally passive:
it never instantiates ``MongoAgentStateHandler`` and never opens a Mongo
connection.  K3/K4 will own the handler lifecycle and register this sink
explicitly; K2 only adds the adapter plus its mapping tests.
"""

from __future__ import annotations

from typing import Any

from .execution_event import CoreExecutionEvent
from .state_store import BaseAgentStateHandler

# stream.terminal is a typed-stream terminator, not a domain event; the
# legacy Mongo collections have no corresponding record type.
_SKIP_EVENT_TYPES: frozenset[str] = frozenset({"stream.terminal"})

# Conservative mapping for events that used to land in agent_executions via
# the default ``_handle_agentic_event`` branch.
_AGENTIC_STATUS: dict[str, str | None] = {
    "step.started": None,
    "step.completed": "success",
    "step.failed": "failed",
    "message.delta": None,
    "checkpoint.written": "success",
    "effect.planned": None,
    "effect.executing": None,
    "effect.succeeded": "success",
    "effect.failed": "failed",
    "effect.uncertain": "uncertain",
    "effect.reconciling": None,
    "effect.reconciled": "success",
    "effect.cancelled": None,
}

_AGENTIC_CATEGORY: dict[str, str] = {
    "step.started": "workflow",
    "step.completed": "workflow",
    "step.failed": "workflow",
    "message.delta": "agent",
    "checkpoint.written": "system",
    "effect.planned": "workflow",
    "effect.executing": "workflow",
    "effect.succeeded": "workflow",
    "effect.failed": "workflow",
    "effect.uncertain": "workflow",
    "effect.reconciling": "workflow",
    "effect.reconciled": "workflow",
    "effect.cancelled": "workflow",
}

# Core event type -> legacy Mongo event type.  ``stream.terminal`` is absent
# on purpose: it must never reach Mongo.
_LEGACY_EVENT_TYPES: dict[str, str] = {
    "tool.invocation_created": "tool_call",
    "tool.invocation_completed": "tool_result",
    "tool.invocation_failed": "tool_result",
    "model.invocation_created": "llm_call",
    "model.invocation_sent": "llm_call",
    "model.invocation_succeeded": "llm_call",
    "model.invocation_failed": "llm_call",
    "model.invocation_unknown": "llm_call",
}

_TRACE_FIELDS = ("trace_id", "span_id")


class LegacyMongoEventSink:
    """Adapter: ``ExecutionEventSink`` -> legacy Mongo state handler.

    ``handler`` is any ``BaseAgentStateHandler`` (``MongoAgentStateHandler``
    in production, a fake in tests).  Events are projected to the smallest
    payload shape the legacy handler already understands; no new collections
    or schemas are introduced.
    """

    def __init__(self, handler: BaseAgentStateHandler) -> None:
        self._handler = handler

    async def emit(self, event: CoreExecutionEvent) -> None:
        if event.type in _SKIP_EVENT_TYPES:
            return
        state_id = str(event.run_id)
        event_type = _LEGACY_EVENT_TYPES.get(event.type, event.type)
        payload = self._payload_for(event)
        await self._handler.handle_event(
            state_id,
            event_type,
            payload,
            base_state=None,
        )

    async def aclose(self) -> None:
        close = getattr(self._handler, "close", None)
        if close is not None:
            await close()

    def _payload_for(self, event: CoreExecutionEvent) -> dict[str, Any]:
        trace = self._trace_envelope(event)
        if event.type.startswith("tool."):
            return {**event.data, **trace}
        if event.type.startswith("model."):
            return self._llm_call_payload(event, trace)
        return self._agentic_payload(event, trace)

    @staticmethod
    def _trace_envelope(event: CoreExecutionEvent) -> dict[str, Any]:
        """Return the old ``_trace`` envelope only when trace data exists."""
        trace_ctx = {
            key: getattr(event, key)
            for key in _TRACE_FIELDS
            if getattr(event, key) is not None
        }
        return {"_trace": trace_ctx} if trace_ctx else {}

    @staticmethod
    def _agentic_payload(
        event: CoreExecutionEvent,
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        """Project to the old AgentEventSchema shape (without its Literals)."""
        payload: dict[str, Any] = {
            "event_id": str(event.event_id),
            "timestamp": event.occurred_at,
            "category": _AGENTIC_CATEGORY[event.type],
            "component": event.type,
            "stage": None,
            "status": _AGENTIC_STATUS[event.type],
            "data": dict(event.data),
        }
        payload.update(trace)
        return payload

    @staticmethod
    def _llm_call_payload(
        event: CoreExecutionEvent,
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        """Align with ``_handle_llm_call_event`` field reads."""
        payload: dict[str, Any] = dict(event.data)
        if event.request_id is not None:
            payload.setdefault("request_id", event.request_id)
        if event.trace_id is not None:
            payload.setdefault("trace_id", event.trace_id)
        if event.span_id is not None:
            payload.setdefault("span_id", event.span_id)
        payload.update(trace)
        return payload
