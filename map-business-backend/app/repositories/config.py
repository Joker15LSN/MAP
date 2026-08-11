"""Repository boundary for persisted admin configuration.

F-01 goal: routers depend on this protocol instead of importing a
module-level :class:`AdminStateStore` instance. The file-backed
implementation lives in ``app.store`` (unchanged for now); R3 will add a
PostgreSQL adapter behind the same protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from ..schemas import AdminState

T = TypeVar("T")


class ConfigRepository(Protocol):
    """Minimal read/update contract used by all admin routers."""

    def load(self) -> AdminState:
        """Return the current validated admin state."""
        ...

    def update(self, updater: Callable[[AdminState], T]) -> tuple[AdminState, T]:
        """Apply ``updater`` under the store's lock and persist the result."""
        ...
