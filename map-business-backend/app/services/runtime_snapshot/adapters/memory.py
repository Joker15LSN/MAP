"""In-memory adapter for runtime snapshots.

Implements the same protocol as the PG adapter (no SQLite, no network)
with an asyncio lock standing in for the current-pointer row lock, so
contract tests can run the exact same scenarios against both adapters.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from ..errors import (
    SnapshotConcurrentModificationError,
    SnapshotNotFoundError,
    SnapshotStateConflictError,
)
from ..schemas import RuntimeProjection, RuntimeSnapshotRecord


class InMemoryRuntimeSnapshotRepository:
    def __init__(self) -> None:
        self._snapshots: dict[uuid.UUID, RuntimeSnapshotRecord] = {}
        self._current_snapshot_id: uuid.UUID | None = None
        self._current_digest: str | None = None
        self._lock = asyncio.Lock()

    async def insert(
        self,
        snapshot_id: uuid.UUID,
        projection: RuntimeProjection,
        digest: str,
        parent_id: uuid.UUID | None,
        status: str,
    ) -> RuntimeSnapshotRecord:
        async with self._lock:
            for existing in self._snapshots.values():
                if existing.digest == digest:
                    return existing
            record = RuntimeSnapshotRecord(
                id=snapshot_id,
                schema_version=projection.schema_version,
                digest=digest,
                parent_id=parent_id,
                status=status,
                created_at=datetime.now(UTC),
                projection=projection,
            )
            self._snapshots[snapshot_id] = record
            return record

    async def get(self, snapshot_id: uuid.UUID) -> RuntimeSnapshotRecord | None:
        async with self._lock:
            return self._snapshots.get(snapshot_id)

    async def get_current(self) -> RuntimeSnapshotRecord | None:
        async with self._lock:
            if self._current_snapshot_id is None:
                return None
            return self._snapshots.get(self._current_snapshot_id)

    async def transition_status(
        self,
        snapshot_id: uuid.UUID,
        from_status: str,
        to_status: str,
    ) -> RuntimeSnapshotRecord:
        async with self._lock:
            record = self._snapshots.get(snapshot_id)
            if record is None:
                raise SnapshotNotFoundError(f"snapshot {snapshot_id} not found")
            if record.status != from_status:
                raise SnapshotStateConflictError(
                    f"snapshot {snapshot_id} cannot transition {from_status} -> {to_status}"
                )
            updated = record.model_copy(update={"status": to_status})
            self._snapshots[snapshot_id] = updated
            return updated

    async def activate(
        self,
        snapshot_id: uuid.UUID,
        expected_current_digest: str | None,
    ) -> RuntimeSnapshotRecord:
        async with self._lock:
            target = self._snapshots.get(snapshot_id)
            if target is None:
                raise SnapshotNotFoundError(f"snapshot {snapshot_id} not found")

            if self._current_snapshot_id == snapshot_id and self._current_digest == target.digest:
                return target

            if self._current_digest != expected_current_digest:
                raise SnapshotConcurrentModificationError(
                    "runtime snapshot current digest changed since the request was read"
                )

            old_active_id = self._current_snapshot_id
            if old_active_id is not None and old_active_id != snapshot_id:
                old_active = self._snapshots.get(old_active_id)
                if old_active is None or old_active.status != "active":
                    raise SnapshotStateConflictError(
                        f"previous active snapshot {old_active_id} is no longer active"
                    )
                self._snapshots[old_active_id] = old_active.model_copy(
                    update={"status": "rolled_back"}
                )

            if target.status not in ("published", "rolled_back"):
                raise SnapshotStateConflictError(
                    f"snapshot {snapshot_id} is not in a status that can be activated"
                )
            updated = target.model_copy(update={"status": "active"})
            self._snapshots[snapshot_id] = updated
            self._current_snapshot_id = snapshot_id
            self._current_digest = target.digest
            return updated
