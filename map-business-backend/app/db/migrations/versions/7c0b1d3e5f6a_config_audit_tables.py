"""config audit tables (FIX-P1-AUDIT-01)

Adds the append-only ``config_audit_events`` (hash-chained) and the
mutable orchestration table ``config_mutations`` (crash recovery only).

Revision ID: 7c0b1d3e5f6a
Revises: 6f8a0c2e4d5b
Create Date: 2026-08-09 03:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7c0b1d3e5f6a"
down_revision = "6f8a0c2e4d5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_audit_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=False),
        sa.Column("actor_subject", sa.String(length=128), nullable=True),
        sa.Column("actor_roles", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("before_version", sa.String(length=32), nullable=True),
        sa.Column("after_version", sa.String(length=32), nullable=True),
        sa.Column("json_patch", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("before_hash", sa.String(length=64), nullable=True),
        sa.Column("after_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("recovered", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("prev_entry_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="map_control",
    )
    op.create_index(
        "ix_audit_events_created", "config_audit_events", ["created_at"], schema="map_control"
    )
    op.create_index(
        "ix_audit_events_resource",
        "config_audit_events",
        ["resource_type", "resource_id"],
        schema="map_control",
    )
    op.create_index(
        "ix_audit_events_actor", "config_audit_events", ["actor_user_id"], schema="map_control"
    )
    op.create_index(
        "ix_audit_events_status", "config_audit_events", ["status"], schema="map_control"
    )

    op.create_table(
        "config_mutations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("resource", sa.String(length=192), nullable=False),
        sa.Column("expected_hash", sa.String(length=64), nullable=False),
        sa.Column("target_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="map_control",
    )
    op.create_index(
        "ix_config_mutations_status", "config_mutations", ["status"], schema="map_control"
    )


def downgrade() -> None:
    op.drop_index("ix_config_mutations_status", table_name="config_mutations", schema="map_control")
    op.drop_table("config_mutations", schema="map_control")
    op.drop_index("ix_audit_events_status", table_name="config_audit_events", schema="map_control")
    op.drop_index("ix_audit_events_actor", table_name="config_audit_events", schema="map_control")
    op.drop_index(
        "ix_audit_events_resource", table_name="config_audit_events", schema="map_control"
    )
    op.drop_index("ix_audit_events_created", table_name="config_audit_events", schema="map_control")
    op.drop_table("config_audit_events", schema="map_control")
