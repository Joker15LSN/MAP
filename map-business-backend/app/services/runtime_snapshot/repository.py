"""Repository protocol for runtime snapshots.

PG and in-memory adapters implement the SAME protocol so the lifecycle
service can be tested without a database and the PG adapter can be
swapped by composition, not inheritance.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from .schemas import RuntimeProjection, RuntimeSnapshotRecord


class RuntimeSnapshotRepository(Protocol):
    async def insert(
        self,
        snapshot_id: uuid.UUID,
        projection: RuntimeProjection,
        digest: str,
        parent_id: uuid.UUID | None,
        status: str,
    ) -> RuntimeSnapshotRecord:
        """Insert a snapshot; ON CONFLICT (digest) DO NOTHING and return the
        existing row when that digest is already stored."""
        ...

    async def get(self, snapshot_id: uuid.UUID) -> RuntimeSnapshotRecord | None:
        """Return the snapshot row or None."""
        ...

    async def get_current(self) -> RuntimeSnapshotRecord | None:
        """Return the active snapshot (current pointer target) or None."""
        ...

    async def transition_status(
        self,
        snapshot_id: uuid.UUID,
        from_status: str,
        to_status: str,
    ) -> RuntimeSnapshotRecord:
        """CAS status transition; rowcount=0 -> SnapshotStateConflictError."""
        ...

    async def activate(
        self,
        snapshot_id: uuid.UUID,
        expected_current_digest: str | None,
    ) -> RuntimeSnapshotRecord:
        """Activate a published/rolled_back snapshot.

        Serializes on the current-pointer row, validates the expected
        pointer digest, marks the previous active snapshot rolled_back,
        marks the target active and updates the pointer. Idempotent when
        the pointer already references the target id/digest.
        """
        ...
