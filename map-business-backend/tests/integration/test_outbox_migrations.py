"""F-03 acceptance: transactional outbox and migration upgrade/downgrade."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db.models import Job, OutboxEvent

pytestmark = pytest.mark.asyncio

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_outbox_commits_with_domain_write(session) -> None:
    """Domain write + outbox insert must commit atomically."""
    job = Job(
        workspace_id=WORKSPACE,
        job_type="test",
        payload_json={"x": 1},
        max_attempts=1,
    )
    session.add(job)
    session.add(
        OutboxEvent(
            aggregate_type="job",
            aggregate_id=str(job.id),
            event_type="job.created",
            payload_json={"job_id": str(job.id)},
        )
    )
    await session.commit()

    result = await session.execute(
        text("SELECT count(*) FROM map_control.outbox_events WHERE event_type = 'job.created'")
    )
    assert result.scalar_one() == 1
    result = await session.execute(text("SELECT count(*) FROM map_control.jobs"))
    assert result.scalar_one() == 1


async def test_rollback_removes_both_domain_and_outbox_rows(session) -> None:
    job = Job(
        workspace_id=WORKSPACE,
        job_type="test",
        payload_json={"x": 1},
        max_attempts=1,
    )
    session.add(job)
    session.add(OutboxEvent(aggregate_type="job", aggregate_id="x", event_type="job.created"))
    await session.rollback()

    result = await session.execute(text("SELECT count(*) FROM map_control.jobs"))
    assert result.scalar_one() == 0
    result = await session.execute(text("SELECT count(*) FROM map_control.outbox_events"))
    assert result.scalar_one() == 0


async def test_migrations_upgrade_downgrade_roundtrip(_engine) -> None:
    """Downgrade one revision, then upgrade to head again (empty DB)."""
    from alembic.config import Config

    project_root = "/Users/liusongnan/MAP/map-business-backend"
    cfg = Config(f"{project_root}/alembic.ini")
    cfg.set_main_option("script_location", f"{project_root}/app/db/migrations")

    # Truncate product tables so downgrade can run.
    async with _engine.connect() as conn:
        await conn.execute(
            text(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'map_control' AND tablename <> 'alembic_version' LOOP "
                "EXECUTE 'TRUNCATE TABLE map_control.' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        )
        await conn.commit()

    # Downgrade one step and upgrade back; both must succeed.
    await _run_alembic(cfg, "downgrade", "-1")
    await _run_alembic(cfg, "upgrade", "head")


async def _run_alembic(cfg, fn: str, arg: str) -> None:
    import asyncio

    from alembic import command

    if fn == "upgrade":
        await asyncio.to_thread(command.upgrade, cfg, arg)
    else:
        await asyncio.to_thread(command.downgrade, cfg, arg)
