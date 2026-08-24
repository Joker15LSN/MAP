"""FIX-P1-DEPLOY-01 acceptance: default Compose configuration works.

- Default settings (no fixture overrides) create conversations with 201.
- /ready proves DB reachability + head revision + default workspace seed.
- Seed migration is idempotent and survives downgrade/upgrade cycles.
"""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_deploy_test_state.json")

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import MIGRATION_DSN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db.session import get_db_session
from app.main import create_app
from app.settings import DEFAULT_WORKSPACE_CODE, DEFAULT_WORKSPACE_ID

pytestmark = pytest.mark.asyncio


def _alembic_config() -> Config:
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = Config(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(project_root, "app/db/migrations"))
    # Migrations always run with the migration role (R2-P1-04).
    os.environ["MAP_CONTROL_MIGRATION_DSN"] = MIGRATION_DSN
    return cfg


async def test_default_settings_create_conversation_201(_engine, session) -> None:
    """E-05 regression: default config must not 404 with workspace not found."""
    from app.settings import Settings

    app = create_app(settings=Settings())  # defaults, no workspace override

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/conversations", json={"mode": "global"})
        assert response.status_code == 201, response.text
        assert response.json()["workspace_id"] == DEFAULT_WORKSPACE_ID


async def test_readiness_fails_without_seed_and_passes_after_seed(_engine, session) -> None:
    """Truncated tables (fixture isolation) -> 503; seeded -> 200."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Fixture truncated everything, so the seed row is gone.
        response = await client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        # R-06: the database check reports a structured ok flag (the fixed
        # 503 envelope shape); True for reachable DB, {"ok": False, ...} on
        # failures.
        assert body["checks"]["database"]["ok"] is True
        assert body["checks"]["seed"]["ok"] is False

        await session.execute(
            text(
                "INSERT INTO map_control.workspaces (id, code, name, status) "
                "VALUES (:wid, :code, '默认工作空间', 'active')"
            ),
            {"wid": uuid.UUID(DEFAULT_WORKSPACE_ID), "code": DEFAULT_WORKSPACE_CODE},
        )
        await session.commit()

        response = await client.get("/ready")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["migration"]["ok"] is True
        assert body["checks"]["seed"]["ok"] is True


async def test_readiness_wrong_workspace_uuid_is_503(_engine, session) -> None:
    """R2-P2-04: seed validity requires the STABLE UUID *and* the code.

    A row with code='default' but a different id must NOT satisfy
    readiness — repro for the code-only check in the second-round review.
    """
    app = create_app()
    wrong_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO map_control.workspaces (id, code, name, status) "
            "VALUES (:wid, :code, '错误 UUID 的种子', 'active')"
        ),
        {"wid": wrong_id, "code": DEFAULT_WORKSPACE_CODE},
    )
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["checks"]["seed"]["ok"] is False
        assert body["checks"]["seed"]["default_workspace_id"] == DEFAULT_WORKSPACE_ID


async def test_seed_migration_idempotent_and_downgrade_upgrade(_engine, session) -> None:
    """upgrade head -> downgrade -1 -> upgrade head leaves exactly one seed."""
    cfg = _alembic_config()

    async def _count() -> int:
        return (
            await session.execute(
                text("SELECT count(*) FROM map_control.workspaces WHERE code = :code"),
                {"code": DEFAULT_WORKSPACE_CODE},
            )
        ).scalar_one()

    # Fixture truncated the seeded row and upgrade is already at head.
    # Downgrade back before the seed migration removes it; resolve the
    # target dynamically so later revisions (e.g. R2-P1-03) never break
    # this round-trip test.
    script = ScriptDirectory.from_config(cfg)
    before_seed = script.get_revision("4c9e1f2a8b3d").down_revision
    await asyncio.to_thread(command.downgrade, cfg, before_seed)
    assert await _count() == 0

    await asyncio.to_thread(command.upgrade, cfg, "head")
    assert await _count() == 1

    # Idempotent: re-running upgrade head must not duplicate the seed.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    assert await _count() == 1

    row = (
        await session.execute(
            text("SELECT id, code FROM map_control.workspaces WHERE code = :code"),
            {"code": DEFAULT_WORKSPACE_CODE},
        )
    ).one()
    assert str(row.id) == DEFAULT_WORKSPACE_ID
