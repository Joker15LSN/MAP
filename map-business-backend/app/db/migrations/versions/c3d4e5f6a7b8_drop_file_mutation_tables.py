"""drop file-backed mutation tables (Step 7 PR-J7b)

``config_mutations`` and ``runtime_snapshot_mutations`` were crash-recovery
orchestration tables for the file-backed AdminState store. J7a made
``apply_change`` one atomic PostgreSQL transaction, so new writes never
create rows here; J7b drops both tables. The drop is idempotent (IF
EXISTS) so it tolerates databases that already removed them.

Downgrade recreates both tables so the full alembic downgrade path
(``base``) stays executable; the recreated tables carry no data.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-30 11:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS map_control.runtime_snapshot_mutations")
    op.execute("DROP TABLE IF EXISTS map_control.config_mutations")


def downgrade() -> None:
    """Recreate the two dropped tables (schema only, no data).

    Kept so ``alembic downgrade base`` can run through this revision; later
    downgrades (which drop these tables' columns/tables) can operate.
    """
    op.create_table(
        "config_mutations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("resource", sa.String(length=192), nullable=False),
        sa.Column("expected_hash", sa.String(length=64), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("actor_user_id", sa.String(length=192), nullable=True),
        sa.Column("actor_subject", sa.String(length=192), nullable=True),
        sa.Column("actor_roles", postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="map_control",
    )
    op.create_index(
        "ix_config_mutations_status",
        "config_mutations",
        ["status"],
        schema="map_control",
    )
    op.create_table(
        "runtime_snapshot_mutations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("resource", sa.String(length=192), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_admin_hash", sa.String(length=64), nullable=False),
        sa.Column("target_admin_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_current_digest", sa.String(length=64), nullable=True),
        sa.Column("target_current_digest", sa.String(length=64), nullable=False),
        sa.Column("target_projection", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.Column("actor_user_id", sa.String(length=192), nullable=True),
        sa.Column("actor_subject", sa.String(length=192), nullable=True),
        sa.Column("actor_roles", postgresql.JSONB(), nullable=True),
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
