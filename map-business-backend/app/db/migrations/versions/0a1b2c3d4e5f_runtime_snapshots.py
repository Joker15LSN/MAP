"""runtime snapshot storage (Step 7 PR-J1)

Adds the immutable ``runtime_snapshots`` table, the singleton
``runtime_snapshot_current`` pointer and the mutable orchestration table
``runtime_snapshot_mutations`` (crash recovery only, mirroring
``config_mutations``).

Immutability is enforced in the database itself: a BEFORE UPDATE trigger
rejects any change to ``id`` / ``projection`` / ``digest`` /
``schema_version`` / ``parent_id``; only ``status`` may move. A partial
unique index guarantees at most one active snapshot globally.

Grants follow the regular-table default privileges (full DML for the app
role); they are repeated explicitly here so the privilege contract is
visible in the same migration that creates the tables.

Revision ID: 0a1b2c3d4e5f
Revises: 7f8e9a0b1c2d
Create Date: 2026-08-24 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0a1b2c3d4e5f"
down_revision = "7f8e9a0b1c2d"
branch_labels = None
depends_on = None

_APP_ROLE = "map"


def _grant_block() -> str:
    tables = (
        "runtime_snapshots",
        "runtime_snapshot_current",
        "runtime_snapshot_mutations",
    )
    lines = []
    for table in tables:
        lines.append(
            f"EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON map_control.{table} TO {_APP_ROLE}';"
        )
    lines.append(
        "EXECUTE 'GRANT EXECUTE ON FUNCTION "
        f"map_control.runtime_snapshots_guard_update() TO {_APP_ROLE}';"
    )
    return "\n        ".join(lines)


def _revoke_block() -> str:
    tables = (
        "runtime_snapshots",
        "runtime_snapshot_current",
        "runtime_snapshot_mutations",
    )
    lines = []
    for table in tables:
        lines.append(
            f"EXECUTE 'REVOKE ALL ON map_control.{table} FROM {_APP_ROLE}';"
        )
    return "\n        ".join(lines)


def upgrade() -> None:
    op.create_table(
        "runtime_snapshots",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("projection", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "parent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("digest", name="uq_runtime_snapshots_digest"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'active', 'rolled_back', 'retired')",
            name="ck_runtime_snapshots_status",
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_runtime_snapshots_schema_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["map_control.runtime_snapshots.id"],
            name="fk_runtime_snapshots_parent_id",
        ),
        schema="map_control",
    )
    op.create_index(
        "uq_runtime_snapshots_one_active",
        "runtime_snapshots",
        ["status"],
        unique=True,
        schema="map_control",
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "runtime_snapshot_current",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "current_snapshot_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("current_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_runtime_snapshot_current_singleton"),
        sa.ForeignKeyConstraint(
            ["current_snapshot_id"],
            ["map_control.runtime_snapshots.id"],
            name="fk_runtime_snapshot_current_snapshot_id",
        ),
        schema="map_control",
    )

    op.create_table(
        "runtime_snapshot_mutations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("resource", sa.String(length=192), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("expected_admin_hash", sa.String(length=64), nullable=False),
        sa.Column("target_admin_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_current_digest", sa.String(length=64), nullable=True),
        sa.Column("target_current_digest", sa.String(length=64), nullable=False),
        sa.Column("target_projection", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "workspace_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("actor_user_id", sa.String(length=192), nullable=True),
        sa.Column("actor_subject", sa.String(length=192), nullable=True),
        sa.Column("actor_roles", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_runtime_snapshot_mutations_status",
        ),
        schema="map_control",
    )
    op.create_index(
        "ix_runtime_snapshot_mutations_status",
        "runtime_snapshot_mutations",
        ["status"],
        schema="map_control",
    )

    op.execute(
        """
CREATE FUNCTION map_control.runtime_snapshots_guard_update()
RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.projection IS DISTINCT FROM OLD.projection
       OR NEW.digest IS DISTINCT FROM OLD.digest
       OR NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
        RAISE EXCEPTION 'runtime_snapshots immutable columns cannot be updated';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER runtime_snapshots_guard_update
BEFORE UPDATE ON map_control.runtime_snapshots
FOR EACH ROW EXECUTE FUNCTION map_control.runtime_snapshots_guard_update();
"""
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
    op.execute(
        f"""
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        {_revoke_block()}
    END IF;
END
$$;
"""
    )
    op.execute("DROP TRIGGER runtime_snapshots_guard_update ON map_control.runtime_snapshots")
    op.execute("DROP FUNCTION map_control.runtime_snapshots_guard_update()")
    op.drop_index(
        "ix_runtime_snapshot_mutations_status",
        table_name="runtime_snapshot_mutations",
        schema="map_control",
    )
    op.drop_table("runtime_snapshot_mutations", schema="map_control")
    op.drop_table("runtime_snapshot_current", schema="map_control")
    op.drop_index(
        "uq_runtime_snapshots_one_active",
        table_name="runtime_snapshots",
        schema="map_control",
    )
    op.drop_table("runtime_snapshots", schema="map_control")
