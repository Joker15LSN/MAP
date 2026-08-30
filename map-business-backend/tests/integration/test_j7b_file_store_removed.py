"""Step 7 PR-J7b acceptance: the file-backed admin state store is gone.

- no compose/Dockerfile/docs reference ``admin_state.json``, ``/app/data``
  or ``MAP_BFF_STATE_FILE``;
- a fresh (truncated) PG database boots with the default AdminState row and
  an active runtime snapshot from the real lifespan seeding path;
- the old orchestration tables ``config_mutations`` and
  ``runtime_snapshot_mutations`` no longer exist after upgrade and startup
  does not touch them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.main import create_app
from app.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2].parent
BFF_ROOT = REPO_ROOT / "map-business-backend"


def test_no_file_store_references_in_deploy_surface() -> None:
    """grep the deploy surface: no admin_state.json/app/data/MAP_BFF_STATE_FILE."""
    needles = ("admin_state.json", "/app/data", "MAP_BFF_STATE_FILE")
    paths = [
        REPO_ROOT / "docs",
        REPO_ROOT / "docker-compose.yml",
        REPO_ROOT / "e2e" / "docker-compose.e2e.yml",
        BFF_ROOT / "Dockerfile",
        REPO_ROOT / ".env.example",
    ]
    hits: list[str] = []
    for path in paths:
        if path.is_dir():
            for file in sorted(path.rglob("*.md")):
                _scan(file, needles, hits)
        elif path.exists():
            _scan(path, needles, hits)
    assert hits == []


def _scan(path: Path, needles: tuple[str, ...], hits: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle in text:
            hits.append(f"{path}: {needle}")


@pytest.mark.asyncio
async def test_fresh_pg_empty_boot_seeds_default_admin_state_and_snapshot(
    _engine, session
) -> None:
    """The lifespan seeds the PG single-row admin state and an active
    snapshot when the database is empty (and never before now)."""
    # `session` fixture already TRUNCATEd every map_control table.
    assert (
        await session.execute(
            text("SELECT count(*) FROM map_control.admin_state")
        )
    ).scalar_one() == 0
    assert (
        await session.execute(
            text("SELECT count(*) FROM map_control.runtime_snapshots")
        )
    ).scalar_one() == 0

    app = create_app(settings=Settings(auth_mode="dev"))
    async with app.router.lifespan_context(app):
        pass

    # The lifespan commits through the global session factory; read back in
    # the test session.
    await session.rollback()
    row = (
        await session.execute(
            text(
                "SELECT id, state_hash FROM map_control.admin_state WHERE id = 1"
            )
        )
    ).one()
    assert row.id == 1
    assert len(row.state_hash) == 64

    snapshots = (
        await session.execute(
            text("SELECT status FROM map_control.runtime_snapshots")
        )
    ).scalars().all()
    assert snapshots == ["active"]
    pointer = (
        await session.execute(
            text(
                "SELECT current_digest FROM map_control.runtime_snapshot_current "
                "WHERE id = 1"
            )
        )
    ).scalar_one()
    assert len(pointer) == 64


@pytest.mark.asyncio
async def test_mutation_tables_are_dropped_and_boot_does_not_touch_them(
    _engine, session
) -> None:
    """J7b drops the file-store crash-recovery tables; boot is unaffected."""
    for table in ("config_mutations", "runtime_snapshot_mutations"):
        exists = (
            await session.execute(
                text(f"SELECT to_regclass('map_control.{table}') IS NOT NULL")
            )
        ).scalar_one()
        assert exists is False

    # Boot after truncate must not fail, and must not recreate those tables.
    app = create_app(settings=Settings(auth_mode="dev"))
    async with app.router.lifespan_context(app):
        pass
    await session.rollback()
    for table in ("config_mutations", "runtime_snapshot_mutations"):
        exists = (
            await session.execute(
                text(f"SELECT to_regclass('map_control.{table}') IS NOT NULL")
            )
        ).scalar_one()
        assert exists is False
