"""Repository boundary for persisted admin configuration.

F-01 goal: routers depend on this protocol instead of importing a
module-level store instance. The durable implementation is
``app.services.runtime_snapshot.adapters.admin_state_pg``; tests may
inject an async fake behind the same protocol.
"""

from __future__ import annotations

from typing import Protocol

from ..schemas import AdminState


class ConfigRepository(Protocol):
    """Minimal async read contract used by all admin read paths."""

    async def load(self) -> AdminState:
        """Return the current validated admin state."""
        ...
