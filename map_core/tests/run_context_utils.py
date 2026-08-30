"""Test helpers for driving production code inside a RunContext."""

from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, Callable, TypeVar

from map_core.service.execution_event import (
    ExecutionEventEmitter,
    InMemoryExecutionEventSink,
    RunContext,
    set_run_context,
)

_T = TypeVar("_T")


def run_with_run_context(factory: Callable[[], Awaitable[_T]]) -> _T:
    """Run an async factory inside a fresh RunContext with an in-memory sink.

    Unit tests that drive production agents/pipelines directly (outside the
    HTTP routers) use this instead of bare ``asyncio.run`` so typed event
    emission has a RunContext and emitted events are observable via the sink.
    """
    run_context = RunContext(run_id=uuid.uuid4())
    sink = InMemoryExecutionEventSink()
    ExecutionEventEmitter.for_context(run_context, sinks=[sink])

    async def _runner() -> _T:
        with set_run_context(run_id=run_context.run_id):
            return await factory()

    return asyncio.run(_runner())


def make_run_context_sink() -> tuple[RunContext, InMemoryExecutionEventSink]:
    """Create and register a RunContext + in-memory sink for manual scenarios."""
    run_context = RunContext(run_id=uuid.uuid4())
    sink = InMemoryExecutionEventSink()
    ExecutionEventEmitter.for_context(run_context, sinks=[sink])
    return run_context, sink
