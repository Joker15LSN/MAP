"""In-flight stream registry (FIX-P1-CONV-01 / R2-P1-01).

The BFF keeps one :class:`StreamRegistry` per app instance (lifespan
managed). Every streaming message registers BEFORE the upstream stream is
created and stays registered across the whole pipeline — upstream pump,
chunk consumption, parser close, finalize — until the outermost ``finally``
unregisters it. ``POST /messages/{id}:stop`` therefore always finds a
mid-stream message.

Stopping is active, not passive: ``abort`` sets the abort event AND cancels
the upstream consumer task, so map_core's request/response is closed at once
instead of only being noticed at the next chunk boundary. Aborting a message
that already reached a terminal state (already unregistered) is a no-op.

Multi-instance deployments: the registry is per-process. Route stop
requests to the instance that owns the stream (sticky routing by
message_id) or replace this registry with a shared cancellation channel;
the DB terminal-state write in the stop endpoint stays correct either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _StreamEntry:
    abort_event: asyncio.Event
    # Upstream consumer task; cancelling it closes the core HTTP stream.
    # None between register() and the pump starting.
    consumer_task: asyncio.Task | None = None


class StreamRegistry:
    """Maps message_id -> (abort event, upstream consumer task)."""

    def __init__(self) -> None:
        self._streams: dict[uuid.UUID, _StreamEntry] = {}

    def register(self, message_id: uuid.UUID) -> asyncio.Event:
        """Register a stream; idempotent per message_id (last one wins)."""
        entry = self._streams.get(message_id)
        if entry is None:
            entry = _StreamEntry(abort_event=asyncio.Event())
            self._streams[message_id] = entry
        return entry.abort_event

    def attach_consumer(self, message_id: uuid.UUID, task: asyncio.Task) -> None:
        """Record the cancellable upstream consumer for an active stream."""
        entry = self._streams.get(message_id)
        if entry is not None:
            entry.consumer_task = task

    def abort(self, message_id: uuid.UUID) -> bool:
        """Request cancellation of a running stream. False if unknown.

        Sets the abort event and actively cancels the upstream consumer so
        the core stream is closed immediately (no side effects after stop),
        rather than waiting for the next chunk boundary.
        """
        entry = self._streams.get(message_id)
        if entry is None:
            return False
        entry.abort_event.set()
        task = entry.consumer_task
        if task is not None and not task.done():
            task.cancel()
            logger.info(
                "stream abort: upstream consumer cancelled message_id=%s",
                message_id,
            )
        return True

    def unregister(self, message_id: uuid.UUID) -> None:
        self._streams.pop(message_id, None)

    def active_count(self) -> int:
        return len(self._streams)

    def active(self) -> list[dict[str, Any]]:
        return [
            {"message_id": str(mid), "aborted": entry.abort_event.is_set()}
            for mid, entry in self._streams.items()
        ]


async def drain_cancelled(task: asyncio.Task | None) -> None:
    """Await a cancelled task swallowing the expected cancellation errors."""
    if task is None:
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
