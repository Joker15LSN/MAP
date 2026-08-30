from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from loguru import logger

from ..agent.base import AgentActionEvent, AgentRequest, AgentResult
from ..agent_runtime import (
    AgentExecutionSpec as _LegacyAgentExecutionSpec,
)
from ..agent_runtime import AgentRuntime as _LegacyAgentRuntime
from .spec import AgentExecutionSpec, resolve_agent_engine


@dataclass
class AgentExecutionHooks:
    """Lifecycle hooks compatible with the existing runtime behaviour.

    The agent argument is the concrete in-process agent instance; callers
    should treat it as an opaque lifecycle handle.
    """

    on_agent_start: (
        Callable[[Any, AgentRequest], Any | Awaitable[Any]] | None
    ) = None
    on_agent_end: (
        Callable[[Any, str, dict[str, Any]], Any | Awaitable[Any]] | None
    ) = None


class AgentRuntime:
    """Public agent execution runtime.

    Callers drive execution through :meth:`execute` (one agent) or
    :meth:`stream` (one or more agents) with an :class:`AgentExecutionSpec`.
    The engine is hidden behind this interface; AgentScope is the default and
    ``legacy`` is only a composition-root rollback switch.
    """

    def __init__(
        self,
        *,
        llm: Any,
        tool_registry: dict[str, Any] | None = None,
        agent_memory_store: Any | None = None,
        logger_: Any | None = None,
        engine: Literal["legacy", "agentscope"] | None = None,
    ) -> None:
        self._legacy_runtime = _LegacyAgentRuntime(
            llm=llm,
            tool_registry=tool_registry,
            agent_memory_store=agent_memory_store,
            logger_=logger_,
        )
        self._engine = engine
        self._logger = logger_ or logger

    # -- compatibility surface used by composition roots (dispatcher, master
    # pipeline and the golden harness assign llm / tool_registry after init) --

    @property
    def llm(self) -> Any:
        return self._legacy_runtime.llm

    @llm.setter
    def llm(self, value: Any) -> None:
        self._legacy_runtime.llm = value

    @property
    def tool_registry(self) -> dict[str, Any]:
        return self._legacy_runtime.tool_registry

    @tool_registry.setter
    def tool_registry(self, value: dict[str, Any]) -> None:
        self._legacy_runtime.tool_registry = value

    @property
    def agent_memory_store(self) -> Any:
        return self._legacy_runtime.agent_memory_store

    # -- engine selection ---------------------------------------------------

    def _resolve_engine(
        self,
        engine: Literal["legacy", "agentscope"] | None,
    ) -> str:
        if engine is not None:
            return resolve_agent_engine(engine)
        if self._engine is not None:
            return resolve_agent_engine(self._engine)
        return resolve_agent_engine(None)

    @staticmethod
    def _to_legacy_spec(
        spec: AgentExecutionSpec,
        engine: str,
    ) -> _LegacyAgentExecutionSpec:
        return _LegacyAgentExecutionSpec(
            **spec.model_dump(),
            engine=engine,
        )

    def _build_agent(
        self,
        spec: AgentExecutionSpec,
        engine: str,
    ) -> Any:
        legacy_spec = self._to_legacy_spec(spec, engine)
        return self._legacy_runtime.build_agent(legacy_spec)

    @staticmethod
    async def _invoke_action_handler(
        handler: Callable[[AgentActionEvent], Awaitable[None] | None] | None,
        event: AgentActionEvent,
    ) -> None:
        if handler is None:
            return
        emitted = handler(event)
        if inspect.isawaitable(emitted):
            await emitted

    @staticmethod
    def _final_answer_event(result: AgentResult) -> AgentActionEvent:
        meta_data = result.meta_data or {}
        return AgentActionEvent(
            agent_code=result.name,
            agent_name=str(meta_data.get("agent_name") or result.name),
            action="final_answer",
            message="模型给出最终答案",
            payload={
                "assistant_content": result.content,
                "success": result.success,
            },
        )

    @staticmethod
    def _with_cancel_marker(
        result: AgentResult,
        cancel: asyncio.Event | None,
    ) -> AgentResult:
        meta_data = dict(result.meta_data or {})
        error_type = (result.data_source or {}).get("error_type")
        if (
            result.error == "cancelled"
            or error_type == "AgentExecutionCancelled"
            or (cancel is not None and cancel.is_set())
        ):
            meta_data["cancelled"] = True
            return result.model_copy(
                update={
                    "success": False,
                    "error": "cancelled",
                    "meta_data": meta_data,
                }
            )
        return result

    # -- public interface ---------------------------------------------------

    async def execute(
        self,
        spec: AgentExecutionSpec,
        request: AgentRequest,
        *,
        action_handler: (
            Callable[[AgentActionEvent], Awaitable[None] | None] | None
        ) = None,
        hooks: AgentExecutionHooks | None = None,
        cancel: asyncio.Event | None = None,
        engine: Literal["legacy", "agentscope"] | None = None,
    ) -> AgentResult:
        """Execute one agent and return its normalized result.

        ``cancel`` is an optional :class:`asyncio.Event`. Once set, the run
        stops before the next model or tool invocation and returns
        ``success=False, error="cancelled"`` with ``meta_data["cancelled"]``.
        """
        resolved_engine = self._resolve_engine(engine)
        agent = self._build_agent(spec, resolved_engine)
        agent.cancel_event = cancel

        final_answer_seen = False

        async def tracked_action_handler(event: AgentActionEvent) -> None:
            nonlocal final_answer_seen
            if event.action == "final_answer":
                final_answer_seen = True
            await self._invoke_action_handler(action_handler, event)

        result = await self._legacy_runtime.run_agent(
            agent,
            request,
            action_handler=(
                tracked_action_handler if action_handler is not None else None
            ),
            hooks=hooks,
        )
        result = self._with_cancel_marker(result, cancel)
        if (
            action_handler is not None
            and not final_answer_seen
            and result.error != "cancelled"
        ):
            await self._invoke_action_handler(
                action_handler,
                self._final_answer_event(result),
            )
        return result

    async def stream(
        self,
        specs: Sequence[AgentExecutionSpec],
        request: AgentRequest,
        *,
        hooks: AgentExecutionHooks | None = None,
        cancel: asyncio.Event | None = None,
        engine: Literal["legacy", "agentscope"] | None = None,
    ) -> AsyncGenerator[AgentResult | AgentActionEvent, None]:
        """Execute a sequence of agents and stream action events + results."""
        resolved_engine = self._resolve_engine(engine)
        agents = [self._build_agent(spec, resolved_engine) for spec in specs]
        for agent in agents:
            agent.cancel_event = cancel
        final_answer_seen: set[str] = set()
        async for item in self._legacy_runtime.run_stream(
            agents,
            request,
            hooks=hooks,
        ):
            if isinstance(item, AgentActionEvent):
                if item.action == "final_answer":
                    final_answer_seen.add(item.agent_code)
                yield item
                continue
            result = self._with_cancel_marker(item, cancel)
            if result.error != "cancelled" and result.name not in final_answer_seen:
                yield self._final_answer_event(result)
                final_answer_seen.add(result.name)
            yield result
