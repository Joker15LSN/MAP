"""JSON -> PG runtime snapshot migration (Step 7 PR-J1 / AC-CONFIG-01)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.runtime_snapshot.digest import projection_digest, snapshot_id_for_digest
from app.services.runtime_snapshot.migrate import migrate_state_file
from app.services.runtime_snapshot.schemas import build_runtime_projection
from app.store import AdminStateStore

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "admin_state_default.json"


@pytest_asyncio.fixture
async def factory(_engine):
    yield async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def _expected_digest() -> str:
    state = AdminStateStore(str(FIXTURE)).load()
    return projection_digest(build_runtime_projection(state))


async def test_migrate_fixture_into_pg_is_idempotent(session, _engine, factory) -> None:
    # `session` fixture guarantees a truncated map_control schema first.
    expected_digest = _expected_digest()
    expected_snapshot_id = snapshot_id_for_digest(expected_digest)

    report = await migrate_state_file(_engine, str(FIXTURE), apply=True)
    assert report.ok is True
    assert report.matching_count == 1
    assert report.digest == expected_digest
    assert report.current_digest == expected_digest
    assert report.snapshot_id == str(expected_snapshot_id)
    assert report.wrote is True

    # Rerun is idempotent: no duplicate snapshot, pointer stays seeded.
    rerun = await migrate_state_file(_engine, str(FIXTURE), apply=True)
    assert rerun.matching_count == 1
    assert rerun.wrote is True  # seed/insert used ON CONFLICT DO NOTHING, not a duplicate

    async with factory() as s:
        count = (
            await s.execute(text("SELECT count(*) FROM map_control.runtime_snapshots"))
        ).scalar_one()
        assert count == 1
        row = (
            await s.execute(
                text(
                    "SELECT id, digest, status FROM map_control.runtime_snapshots "
                    "ORDER BY created_at LIMIT 1"
                )
            )
        ).one()
        assert row.digest == expected_digest
        assert str(row.id) == str(expected_snapshot_id)
        assert row.status == "active"
        pointer = (
            await s.execute(
                text(
                    "SELECT current_snapshot_id, current_digest "
                    "FROM map_control.runtime_snapshot_current WHERE id = 1"
                )
            )
        ).one()
        assert pointer.current_digest == expected_digest
        assert str(pointer.current_snapshot_id) == str(expected_snapshot_id)


async def test_migrate_check_mode_does_not_write(session, _engine) -> None:
    report = await migrate_state_file(_engine, str(FIXTURE), apply=False)
    assert report.ok is True
    assert report.wrote is False

    session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as s:
        count = (
            await s.execute(text("SELECT count(*) FROM map_control.runtime_snapshots"))
        ).scalar_one()
        assert count == 0
        pointer = (
            await s.execute(text("SELECT count(*) FROM map_control.runtime_snapshot_current"))
        ).scalar_one()
        assert pointer == 0


async def test_migrate_conflicting_digest_refuses_to_write(session, _engine) -> None:
    # Seed a snapshot whose digest differs from the fixture-derived digest.
    async with async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)() as s:
        await s.execute(
            text(
                "INSERT INTO map_control.runtime_snapshots "
                "(id, schema_version, projection, digest, status) "
                "VALUES (gen_random_uuid(), 1, '{}', :digest, 'draft')"
            ),
            {"digest": "0" * 64},
        )
        await s.commit()

    with pytest.raises(SystemExit) as exc_info:
        await migrate_state_file(_engine, str(FIXTURE), apply=True)
    assert exc_info.value.code == 2
