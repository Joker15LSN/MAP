from __future__ import annotations

import asyncio
import secrets
from abc import abstractmethod
from typing import Any, AsyncGenerator, Awaitable

from ...schema.state_event_schema import ToolCallData, ToolResultData
from ...utils.llm_engine import LLMEngine
from ..state_store import (
    GlobalAgentStateStore,
    fire_and_forget,
    safe_serialize,
)
from .base import AgentExecutionCancelled, AgentRequest, AgentResult, BaseAgent


class TraceableAgent(BaseAgent):
    """
    ReAct Agent that extends BaseAgent with enhanced state management
    and runtime event recording capabilities for chain-of-thought reasoning and tool interactions.
    """

    def __init__(
        self, llm: LLMEngine, name: str = "TraceableAgent", aid: str | None = None
    ) -> None:
        super().__init__(llm, name=name)
        self.state_store: GlobalAgentStateStore | None = None
        self.state_id: str | None = None
        self.aid = aid or secrets.token_hex(10)
        self.parid: str = "-"
        self.cancel_event: asyncio.Event | None = None

    def check_cancelled(self) -> None:
        """Raise :class:`AgentExecutionCancelled` when the cancel event is set."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AgentExecutionCancelled("cancelled")

    def set_execution_context(self, state_store: GlobalAgentStateStore, state_id: str):
        """Inject global agent state for the agent instance."""
        self.state_store = state_store
        self.state_id = state_id

    def _record_runtime_event(self, event_type: str, payload: dict[str, Any]):
        """
        Low-level recording method: automatically checks if store exists and sends in fire-and-forget mode.
        """
        if self.state_store and self.state_id:
            full_payload = {
                "agent_code": self.name,
                "agent_name": self.agent_display_name,
                "agent_id": self.agent_id,
                **payload,
            }

            fire_and_forget(
                self.state_store.record_event(
                    state_id=self.state_id, event_type=event_type, payload=full_payload
                )
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
