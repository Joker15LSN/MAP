"""pin runtime snapshot id/digest on runs (Step 7 PR-J5)

Adds two nullable columns to ``map_control.runs``:

- ``runtime_snapshot_id``  UUID NULL -> ``runtime_snapshots.id``
- ``runtime_snapshot_digest`` VARCHAR(64) NULL

Old runs keep NULL (interpretable history); new runs must set both
together, enforced by the ``(runtime_snapshot_id IS NULL) =
(runtime_snapshot_digest IS NULL)`` check. The application layer
guarantees the non-NULL pair for new runs.

Grants follow the guarded-DO-block convention: the app role gets
SELECT/UPDATE on ``runs`` explicitly so the two new columns are readable
and writable even when the role was provisioned before this migration.

Revision ID: a7b8c9d0e1f2
Revises: 0a1b2c3d4e5f
Create Date: 2026-08-24 11:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None

_APP_ROLE = "map"


def _grant_block() -> str:
    return f"EXECUTE 'GRANT SELECT, UPDATE ON map_control.runs TO {_APP_ROLE}';"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "runtime_snapshot_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        schema="map_control",
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_snapshot_digest",
            sa.String(length=64),
            nullable=True,
        ),
        schema="map_control",
    )
    op.create_foreign_key(
        "fk_runs_runtime_snapshot_id",
        "runs",
        "runtime_snapshots",
        ["runtime_snapshot_id"],
        ["id"],
        source_schema="map_control",
        referent_schema="map_control",
    )
    op.create_check_constraint(
        "ck_runs_runtime_snapshot_pair",
        "runs",
        "(runtime_snapshot_id IS NULL) = (runtime_snapshot_digest IS NULL)",
        schema="map_control",
    )

    op.execute(
        f"""
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        {_grant_block()}
    END IF;
END
$$;
"""
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_runs_runtime_snapshot_pair",
        "runs",
        schema="map_control",
        type_="check",
    )
    op.drop_constraint(
        "fk_runs_runtime_snapshot_id",
        "runs",
        schema="map_control",
        type_="foreignkey",
    )
    op.drop_column("runs", "runtime_snapshot_digest", schema="map_control")
    op.drop_column("runs", "runtime_snapshot_id", schema="map_control")
