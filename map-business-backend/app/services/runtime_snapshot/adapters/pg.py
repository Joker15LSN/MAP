"""PostgreSQL adapter for runtime snapshots.

All state changes run in the caller's transaction; this adapter never
commits. The current-pointer row is the serialization point for
activations: ``SELECT ... FOR UPDATE`` on ``runtime_snapshot_current``
makes sure only one activation wins when two callers race with the same
expected pointer digest.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ....db.models import RuntimeSnapshot, RuntimeSnapshotCurrent
from ..errors import (
    SnapshotConcurrentModificationError,
    SnapshotNotFoundError,
    SnapshotStateConflictError,
)
from ..schemas import RuntimeProjection, RuntimeSnapshotRecord

_SNAPSHOT_COLUMNS = (
    "id",
    "schema_version",
    "projection",
    "digest",
    "status",
    "parent_id",
    "created_at",
)


def _record_from_mapping(row) -> RuntimeSnapshotRecord:
    return RuntimeSnapshotRecord(
        id=row["id"],
        schema_version=row["schema_version"],
        digest=row["digest"],
        parent_id=row["parent_id"],
        status=row["status"],
        created_at=row["created_at"],
        projection=RuntimeProjection.model_validate(row["projection"]),
    )


class PgRuntimeSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        snapshot_id: uuid.UUID,
        projection: RuntimeProjection,
        digest: str,
        parent_id: uuid.UUID | None,
        status: str,
    ) -> RuntimeSnapshotRecord:
        table = RuntimeSnapshot.__table__
        stmt = (
            pg_insert(table)
            .values(
                id=snapshot_id,
                schema_version=projection.schema_version,
                projection=projection.model_dump(mode="json"),
                digest=digest,
                parent_id=parent_id,
                status=status,
            )
            .on_conflict_do_nothing(index_elements=["digest"])
            .returning(*[table.c[name] for name in _SNAPSHOT_COLUMNS])
        )
        row = (await self._session.execute(stmt)).mappings().one_or_none()
        if row is not None:
            return _record_from_mapping(row)

        existing = (
            (
                await self._session.execute(
                    select(RuntimeSnapshot).where(RuntimeSnapshot.digest == digest)
                )
            )
            .scalars()
            .one()
        )
        return _record_from_orm(existing)

    async def get(self, snapshot_id: uuid.UUID) -> RuntimeSnapshotRecord | None:
        row = (
            (
                await self._session.execute(
                    select(RuntimeSnapshot).where(RuntimeSnapshot.id == snapshot_id)
                )
            )
            .scalars()
            .one_or_none()
        )
        return _record_from_orm(row) if row is not None else None

    async def get_current(self) -> RuntimeSnapshotRecord | None:
        row = (
            (
                await self._session.execute(
                    select(RuntimeSnapshot)
                    .join(
                        RuntimeSnapshotCurrent,
                        RuntimeSnapshotCurrent.current_snapshot_id == RuntimeSnapshot.id,
                    )
                    .where(RuntimeSnapshotCurrent.id == 1)
                )
            )
            .scalars()
            .one_or_none()
        )
        return _record_from_orm(row) if row is not None else None

    async def transition_status(
        self,
        snapshot_id: uuid.UUID,
        from_status: str,
        to_status: str,
    ) -> RuntimeSnapshotRecord:
        table = RuntimeSnapshot.__table__
        stmt = (
            update(table)
            .where(table.c.id == snapshot_id, table.c.status == from_status)
            .values(status=to_status)
            .returning(*[table.c[name] for name in _SNAPSHOT_COLUMNS])
        )
        row = (await self._session.execute(stmt)).mappings().one_or_none()
        if row is None:
            raise SnapshotStateConflictError(
                f"snapshot {snapshot_id} cannot transition {from_status} -> {to_status}"
            )
        return _record_from_mapping(row)

    async def activate(
        self,
        snapshot_id: uuid.UUID,
        expected_current_digest: str | None,
    ) -> RuntimeSnapshotRecord:
        snapshot_table = RuntimeSnapshot.__table__
        pointer_table = RuntimeSnapshotCurrent.__table__

        target = (
            (
                await self._session.execute(
                    select(RuntimeSnapshot).where(RuntimeSnapshot.id == snapshot_id)
                )
            )
            .scalars()
            .one_or_none()
        )
        if target is None:
            raise SnapshotNotFoundError(f"snapshot {snapshot_id} not found")

        pointer = (
            (
                await self._session.execute(
                    select(RuntimeSnapshotCurrent)
                    .where(RuntimeSnapshotCurrent.id == 1)
                    .with_for_update()
                )
            )
            .scalars()
            .one_or_none()
        )

        # Idempotent: the pointer already references this id/digest.
        if (
            pointer is not None
            and pointer.current_snapshot_id == snapshot_id
            and pointer.current_digest == target.digest
        ):
            return _record_from_orm(target)

        if pointer is None:
            if expected_current_digest is not None:
                raise SnapshotConcurrentModificationError(
                    "current pointer is empty but an existing pointer digest was expected"
                )
        elif pointer.current_digest != expected_current_digest:
            raise SnapshotConcurrentModificationError(
                "runtime snapshot current digest changed since the request was read"
            )

        old_active_id = pointer.current_snapshot_id if pointer is not None else None
        if old_active_id is not None and old_active_id != snapshot_id:
            old_update = (
                update(snapshot_table)
                .where(snapshot_table.c.id == old_active_id, snapshot_table.c.status == "active")
                .values(status="rolled_back")
            )
            old_result = await self._session.execute(old_update)
            if old_result.rowcount == 0:
                raise SnapshotStateConflictError(
                    f"previous active snapshot {old_active_id} is no longer active"
                )

        target_update = (
            update(snapshot_table)
            .where(
                snapshot_table.c.id == snapshot_id,
                snapshot_table.c.status.in_(("published", "rolled_back")),
            )
            .values(status="active")
            .returning(*[snapshot_table.c[name] for name in _SNAPSHOT_COLUMNS])
        )
        updated = (await self._session.execute(target_update)).mappings().one_or_none()
        if updated is None:
            raise SnapshotStateConflictError(
                f"snapshot {snapshot_id} is not in a status that can be activated"
            )

        now = datetime.now(UTC)
        if pointer is None:
            await self._session.execute(
                pg_insert(pointer_table).values(
                    id=1,
                    current_snapshot_id=snapshot_id,
                    current_digest=target.digest,
                    updated_at=now,
                )
            )
        else:
            await self._session.execute(
                update(pointer_table)
                .where(pointer_table.c.id == 1)
                .values(
                    current_snapshot_id=snapshot_id,
                    current_digest=target.digest,
                    updated_at=now,
                )
            )
        return _record_from_mapping(updated)

    async def seed_current_pointer(self, snapshot_id: uuid.UUID, digest: str) -> None:
        """Seed the singleton pointer (ON CONFLICT DO NOTHING).

        Used by the JSON -> PG migration command so the first active
        snapshot becomes the current pointer without overwriting a newer
        activation.
        """
        await self._session.execute(
            pg_insert(RuntimeSnapshotCurrent.__table__)
            .values(id=1, current_snapshot_id=snapshot_id, current_digest=digest)
            .on_conflict_do_nothing(index_elements=["id"])
        )


def _record_from_orm(row: RuntimeSnapshot) -> RuntimeSnapshotRecord:
    return RuntimeSnapshotRecord(
        id=row.id,
        schema_version=row.schema_version,
        digest=row.digest,
        parent_id=row.parent_id,
        status=row.status,
        created_at=row.created_at,
        projection=RuntimeProjection.model_validate(row.projection),
    )
