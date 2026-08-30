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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
#
# R2-P1-04: the suite exercises the REAL compose role separation:
#   APP_DSN       - the backend/worker business role (non-superuser, DML)
#   MIGRATION_DSN - the migration role (Alembic DDL)
#   ADMIN_DSN     - the bootstrap/admin role (test isolation: TRUNCATE)
# ---------------------------------------------------------------------------

APP_DSN = os.getenv(
    "MAP_CONTROL_TEST_DSN",
    "postgresql+asyncpg://map:map@127.0.0.1:15432/map",
)
TEST_DSN = APP_DSN  # historical alias used across integration tests
MIGRATION_DSN = os.getenv(
    "MAP_CONTROL_MIGRATION_TEST_DSN",
    "postgresql+asyncpg://map_migrator:map-migrator-local@127.0.0.1:15432/map",
)
ADMIN_DSN = os.getenv(
    "MAP_CONTROL_ADMIN_TEST_DSN",
    "postgresql+asyncpg://map_admin:map-admin-local@127.0.0.1:15432/map",
)

# Non-integration tests (test_principal, test_flow_*, ...) exercise the app's
# global engine through create_app(). Production code deliberately has no
# repository DSN default (P0-SEC-01), so the test suite injects the local
# test DSN explicitly; CI overrides it via MAP_CONTROL_TEST_DSN if needed.
os.environ.setdefault("MAP_CONTROL_DB_DSN", APP_DSN)


async def seed_pg_admin_state(session, state=None) -> bool:
    """Seed the PG single-row admin state once (idempotent helper).

    Tests that build an app with ``get_db_session`` overridden to a
    session must call this BEFORE exercising routes that read admin state;
    production seeds it in the lifespan, but ``ASGITransport`` does not
    run lifespan handlers.
    """
    from app.schemas import AdminState
    from app.services.runtime_snapshot.adapters.admin_state_pg import (
        PgAdminStateRepository,
    )

    repo = PgAdminStateRepository(session)
    inserted = await repo.seed_if_empty(state or AdminState.default())
    await session.commit()
    return inserted


async def run_alembic_upgrade() -> None:
    """Upgrade to head in a worker thread (alembic runs its own loop)."""
    from alembic import command
    from alembic.config import Config

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(project_root, "app/db/migrations"))
    os.environ["MAP_CONTROL_MIGRATION_DSN"] = MIGRATION_DSN
    await asyncio.to_thread(command.upgrade, cfg, "head")


def _test_engine(dsn: str) -> AsyncEngine:
    # pool_pre_ping mirrors production build_engine(): client disconnects
    # cancel mid-DB-await and can kill a pooled asyncpg connection; a dead
    # connection must never be handed out again.
    return create_async_engine(dsn, pool_pre_ping=True)


@pytest_asyncio.fixture
async def _engine():
    await run_alembic_upgrade()
    engine = _test_engine(APP_DSN)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(_engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    # Test isolation runs with the admin role: the app role intentionally
    # has no TRUNCATE (append-only audit contract, R2-P1-04).
    admin_engine = _test_engine(ADMIN_DSN)
    async with admin_engine.begin() as admin_conn:
        await admin_conn.execute(
            text(
                "DO $$ DECLARE r RECORD; BEGIN "
                "FOR r IN SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'map_control' AND tablename <> 'alembic_version' LOOP "
                "EXECUTE 'TRUNCATE TABLE map_control.' || quote_ident(r.tablename) || ' CASCADE'; "
                "END LOOP; END $$;"
            )
        )
    await admin_engine.dispose()
    async with factory() as s:
        yield s
        await s.rollback()


"""Integration test fixtures: real PostgreSQL via docker compose.

The suite expects a fresh compose database reachable at ``MAP_CONTROL_TEST_DSN``
(app role, defaults to the compose-local instance on 127.0.0.1:15432).
Migrations are applied with Alembic via the migration role before each test
(idempotent); every test truncates the map_control tables with the admin
role for isolation.

The engine fixture is function-scoped and async so the asyncpg connection
is created and closed on the same event loop as the test.
"""
