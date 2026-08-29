"""Dual-adapter contract tests for runtime snapshot repositories (PR-J2)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RuntimeSnapshot
from app.services.runtime_snapshot.adapters.memory import (
    InMemoryRuntimeSnapshotRepository,
)
from app.services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from app.services.runtime_snapshot.digest import (
    projection_digest,
    snapshot_id_for_digest,
)
from app.services.runtime_snapshot.errors import (
    SnapshotConcurrentModificationError,
    SnapshotStateConflictError,
)
from app.services.runtime_snapshot.schemas import RuntimeProjection

pytestmark = pytest.mark.asyncio


def _projection(tag: str) -> RuntimeProjection:
    return RuntimeProjection(
        schema_version=1,
        scene_selection={"tag": tag},
        dispatch_config={},
        flow_policy={},
        scenario_packs=[],
        flow_skill_descriptors=[],
    )


async def _exercise_contract(repo) -> tuple[uuid.UUID, str]:
    projection = _projection("contract")
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)

    inserted = await repo.insert(snapshot_id, projection, digest, None, "draft")
    assert inserted.status == "draft"
    assert inserted.digest == digest
    assert inserted.schema_version == 1

    assert (await repo.get(snapshot_id)).id == snapshot_id
    assert await repo.get(uuid.uuid4()) is None

    # ON CONFLICT (digest) returns the existing row, never a duplicate.
    duplicate = await repo.insert(uuid.uuid4(), projection, digest, None, "published")
    assert duplicate.id == snapshot_id
    assert duplicate.status == "draft"

    published = await repo.transition_status(snapshot_id, "draft", "published")
    assert published.status == "published"
    with pytest.raises(SnapshotStateConflictError):
        await repo.transition_status(snapshot_id, "draft", "published")

    active = await repo.activate(snapshot_id, None)
    assert active.status == "active"
    current = await repo.get_current()
    assert current.id == snapshot_id
    assert current.digest == digest

    return snapshot_id, digest


async def test_pg_contract(session) -> None:
    repo = PgRuntimeSnapshotRepository(session)
    await _exercise_contract(repo)
    await session.commit()


async def test_memory_contract() -> None:
    repo = InMemoryRuntimeSnapshotRepository()
    await _exercise_contract(repo)


async def test_pg_concurrent_activation_one_winner(session, _engine) -> None:
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    projections = [_projection(tag) for tag in ("one", "two", "three")]
    digests = [projection_digest(p) for p in projections]
    ids = [snapshot_id_for_digest(d) for d in digests]

    async with factory() as s:
        repo = PgRuntimeSnapshotRepository(s)
        await repo.insert(ids[0], projections[0], digests[0], None, "published")
        await repo.activate(ids[0], None)
        await repo.insert(ids[1], projections[1], digests[1], ids[0], "published")
        await repo.insert(ids[2], projections[2], digests[2], ids[0], "published")
        await s.commit()

    async def attempt(snapshot_id: uuid.UUID):
        async with factory() as s:
            repo = PgRuntimeSnapshotRepository(s)
            try:
                record = await repo.activate(snapshot_id, digests[0])
                await s.commit()
                return record, None
            except SnapshotConcurrentModificationError as exc:
                await s.rollback()
                return None, exc

    (first, first_exc), (second, second_exc) = await asyncio.gather(
        attempt(ids[1]), attempt(ids[2])
    )
    assert (first is None) ^ (second is None)
    assert (first_exc is None) ^ (second_exc is None)

    async with factory() as s:
        repo = PgRuntimeSnapshotRepository(s)
        current = await repo.get_current()
        assert current.digest in (digests[1], digests[2])
        previous = await repo.get(ids[0])
        assert previous.status == "rolled_back"


async def test_memory_concurrent_activation_one_winner() -> None:
    repo = InMemoryRuntimeSnapshotRepository()
    projections = [_projection(tag) for tag in ("one", "two", "three")]
    digests = [projection_digest(p) for p in projections]
    ids = [snapshot_id_for_digest(d) for d in digests]

    await repo.insert(ids[0], projections[0], digests[0], None, "published")
    await repo.activate(ids[0], None)
    await repo.insert(ids[1], projections[1], digests[1], ids[0], "published")
    await repo.insert(ids[2], projections[2], digests[2], ids[0], "published")

    async def attempt(snapshot_id: uuid.UUID):
        try:
            return await repo.activate(snapshot_id, digests[0]), None
        except SnapshotConcurrentModificationError as exc:
            return None, exc

    (first, first_exc), (second, second_exc) = await asyncio.gather(
        attempt(ids[1]), attempt(ids[2])
    )
    assert (first is None) ^ (second is None)
    assert (first_exc is None) ^ (second_exc is None)

    current = await repo.get_current()
    assert current.digest in (digests[1], digests[2])
    previous = await repo.get(ids[0])
    assert previous.status == "rolled_back"


async def test_pg_trigger_rejects_immutable_column_update(session) -> None:
    projection = _projection("immutable")
    digest = projection_digest(projection)
    snapshot_id = snapshot_id_for_digest(digest)
    repo = PgRuntimeSnapshotRepository(session)
    await repo.insert(snapshot_id, projection, digest, None, "draft")
    await session.commit()

    with pytest.raises(DBAPIError):
        await session.execute(
            text(
                "UPDATE map_control.runtime_snapshots SET projection = '{}' "
                "WHERE id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
    await session.rollback()

    # Status-only update still works (the trigger permits it).
    await session.execute(
        text(
            "UPDATE map_control.runtime_snapshots SET status = 'published' "
            "WHERE id = :snapshot_id"
        ),
        {"snapshot_id": snapshot_id},
    )
    await session.commit()
    row = await session.get(RuntimeSnapshot, snapshot_id)
    assert row.status == "published"
