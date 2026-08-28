"""Public Agent Execution module.

Callers drive agent runs through this module and only need to learn:

- :class:`AgentExecutionSpec` (engine is deliberately absent from this model),
- :class:`AgentRequest` / :class:`AgentResult` / :class:`AgentActionEvent`,
- :class:`AgentRuntime` (``execute`` / ``stream`` / ``set_execution_context``),
- :class:`AgentExecutionHooks` and the optional ``asyncio.Event`` cancel switch.

Engine selection (``MAP_AGENT_ENGINE`` or a composition-root override) is an
internal rollback switch. AgentScope is the default engine.
"""

from ..agent.base import AgentActionEvent, AgentRequest, AgentResult
from .runtime import AgentExecutionHooks, AgentRuntime
from .spec import AgentExecutionSpec

__all__ = [
    "AgentActionEvent",
    "AgentExecutionHooks",
    "AgentExecutionSpec",
    "AgentRequest",
    "AgentResult",
    "AgentRuntime",
]
