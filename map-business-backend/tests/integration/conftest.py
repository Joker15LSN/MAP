"""Integration test fixtures: real PostgreSQL via docker compose.

The suite expects a PostgreSQL reachable at ``MAP_CONTROL_TEST_DSN``
(defaults to the compose-local instance on 127.0.0.1:15432). Migrations are
applied with Alembic before each test (idempotent); every test truncates
the map_control tables for isolation.

The engine fixture is function-scoped and async so the asyncpg connection
is created and closed on the same event loop as the test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DSN = os.getenv(
    "MAP_CONTROL_TEST_DSN",
    "postgresql+asyncpg://map:map@127.0.0.1:15432/map",
)


async def run_alembic_upgrade() -> None:
    """Upgrade to head in a worker thread (alembic runs its own loop)."""
    from alembic import command
    from alembic.config import Config

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = Config(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(project_root, "app/db/migrations"))
    os.environ["MAP_CONTROL_MIGRATION_DSN"] = TEST_DSN
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest_asyncio.fixture
async def _engine():
    await run_alembic_upgrade()
    engine = create_async_engine(TEST_DSN)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(_engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        # Truncate all map_control tables (keep alembic_version).
        await s.execute(
            text(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'map_control' AND tablename <> 'alembic_version' LOOP "
                "EXECUTE 'TRUNCATE TABLE map_control.' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        )
        await s.commit()
        yield s
        await s.rollback()
