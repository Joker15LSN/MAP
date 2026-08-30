from __future__ import annotations

import asyncio
import secrets
from abc import abstractmethod
from typing import Any, AsyncGenerator, Awaitable

from ...schema.state_event_schema import ToolCallData, ToolResultData
from ...utils.model_invocation import ModelInvocation
from ...utils.serialization import safe_serialize
from ..execution_event import ExecutionEventEmitter
from .base import AgentExecutionCancelled, AgentRequest, AgentResult, BaseAgent


class TraceableAgent(BaseAgent):
    """
    ReAct Agent that extends BaseAgent with enhanced state management
    and runtime event recording capabilities for chain-of-thought reasoning and tool interactions.
    """

    def __init__(
        self, llm: ModelInvocation, name: str = "TraceableAgent", aid: str | None = None
    ) -> None:
        super().__init__(llm, name=name)
        self.aid = aid or secrets.token_hex(10)
        self.parid: str = "-"
        self.cancel_event: asyncio.Event | None = None

    def check_cancelled(self) -> None:
        """Raise :class:`AgentExecutionCancelled` when the cancel event is set."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AgentExecutionCancelled("cancelled")

    def _record_runtime_event(self, event_type: str, payload: dict[str, Any]):
        """Low-level typed recording method using the request emitter."""
        full_payload = {
            "agent_code": self.name,
            "agent_name": self.agent_display_name,
            "agent_id": self.agent_id,
            **payload,
        }
        emitter = ExecutionEventEmitter.current()
        if event_type == "tool_call":
            emitter.emit("tool.invocation_created", data=full_payload)
            return
        if event_type == "tool_result":
            output = full_payload.get("output")
            failed = isinstance(output, dict) and (
                output.get("success") is False or output.get("error") is not None
            )
            emitter.emit(
                "tool.invocation_failed" if failed else "tool.invocation_completed",
                data=full_payload,
            )
            return
        if event_type in {"agent_thought", "agent_message"}:
            emitter.emit(
                "message.delta",
                data={"component": event_type, **full_payload},
            )
            return
        status = full_payload.get("status") if isinstance(full_payload, dict) else None
        emitter.emit(
            "step.failed" if status == "failed" else "step.completed",
            data={"component": event_type, **full_payload},
        )

    def record_agentic_event(self, event_type: str, payload: dict[str, Any]):
        """Record a structured agent lifecycle or LLM event."""
        self._record_runtime_event(event_type, safe_serialize(payload))

    def record_thought(self, thought: str, step_index: int | None = None):
        """Record the agent's thought process."""
        self._record_runtime_event(
            "agent_thought", {"content": thought, "step": step_index}
        )

    def record_tool_call(
        self,
        tool_name: str,
        tool_args: dict | str,
        step_index: int | None = None,
        meta: dict[str, Any] | None = None,  # include tool/agent state, e.g. aid, parid
    ):
        """Record tool invocation actions."""
        payload = {
            "tool": tool_name,
            "args": safe_serialize(tool_args),
            "step": step_index,
            **(safe_serialize(meta) if meta else {}),
        }
        self._record_runtime_event("tool_call", ToolCallData(**payload).model_dump())

    def record_tool_result(
        self,
        tool_name: str,
        result: Any,
        step_index: int | None = None,
        meta: dict[str, Any] | None = None,
    ):
        """Record tool execution results."""
        payload = {
            "tool": tool_name,
            "output": self._serialize_tool_result(result),
            "step": step_index,
            **(safe_serialize(meta) if meta else {}),
        }
        self._record_runtime_event(
            "tool_result", ToolResultData(**payload).model_dump()
        )

    def _serialize_tool_result(self, result: Any) -> Any:
        """Serialize tool output for event storage; subclasses may override."""
        return safe_serialize(result)

    def record_message(self, role: str, content: str):
        """Record a generated conversation message."""
        self._record_runtime_event("agent_message", {"role": role, "content": content})

    @abstractmethod
    def run(
        self, request: AgentRequest, *, parid: str = "-"
    ) -> Awaitable[AgentResult | AsyncGenerator[str, None] | Any]:
        """
        Execute the ReAct agent logic. Subclasses should override this method
        to implement specific reasoning and action patterns.
        """
        raise NotImplementedError("Subclasses must implement the run method")
