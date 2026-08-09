from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from otel_env import OTEL_ENV_VARS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Hermetic suite guard. app.main is imported at collection time and its
# module-level configure_bff_telemetry() reads OTel env vars, so a developer
# shell or CI runner that exports MAP_OTEL_ENABLED=true / OTEL_SDK_DISABLED=true
# / MAP_OTEL_EXCLUDED_PATHS=/api/chat would silently change what the suite
# exercises. Drop every var BEFORE collection; the autouse fixture below then
# keeps each test deterministic and restores the host env afterwards.
for _var in OTEL_ENV_VARS:
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def _hermetic_otel_env(monkeypatch):
    """Remove host OTel vars for every test; monkeypatch restores them after.

    Note: we delete (never set) OTEL_SDK_DISABLED — setting it to "true"
    would also silence the explicit instrumentation that test_bff_spans
    installs (SDK kill-switch semantics) and break those tests.
    """
    for var in OTEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Shared DB fixtures (used by tests/integration and tests/e2e).
# ---------------------------------------------------------------------------

TEST_DSN = os.getenv(
    "MAP_CONTROL_TEST_DSN",
    "postgresql+asyncpg://map:map@127.0.0.1:15432/map",
)


async def run_alembic_upgrade() -> None:
    """Upgrade to head in a worker thread (alembic runs its own loop)."""
    from alembic import command
    from alembic.config import Config

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


"""Integration test fixtures: real PostgreSQL via docker compose.

The suite expects a PostgreSQL reachable at ``MAP_CONTROL_TEST_DSN``
(defaults to the compose-local instance on 127.0.0.1:15432). Migrations are
applied with Alembic before each test (idempotent); every test truncates
the map_control tables for isolation.

The engine fixture is function-scoped and async so the asyncpg connection
is created and closed on the same event loop as the test.
"""

TEST_DSN = os.getenv(
    "MAP_CONTROL_TEST_DSN",
    "postgresql+asyncpg://map:map@127.0.0.1:15432/map",
)
