"""In-flight stream registry (FIX-P1-CONV-01).

The BFF keeps one :class:`StreamRegistry` per app instance (lifespan
managed). Every streaming message registers an abort event; ``POST
/messages/{id}:stop`` sets it so the stream loop cancels the downstream
request and finalizes ``stopped``. Aborting a message that already reached
a terminal state is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class StreamRegistry:
    """Maps message_id -> abort event for running streams."""

    def __init__(self) -> None:
        self._streams: dict[uuid.UUID, asyncio.Event] = {}

    def register(self, message_id: uuid.UUID) -> asyncio.Event:
        event = asyncio.Event()
        self._streams[message_id] = event
        return event

    def abort(self, message_id: uuid.UUID) -> bool:
        """Request cancellation of a running stream. False if unknown."""
        event = self._streams.get(message_id)
        if event is None:
            return False
        event.set()
        return True

    def unregister(self, message_id: uuid.UUID) -> None:
        self._streams.pop(message_id, None)

    def active_count(self) -> int:
        return len(self._streams)

    def active(self) -> list[dict[str, Any]]:
        return [
            {"message_id": str(mid), "aborted": ev.is_set()} for mid, ev in self._streams.items()
        ]
