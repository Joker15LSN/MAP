"""Step 4 / PR-F1: add the canonical run reference to messages.

New turns are written atomically with their Run by the Turn application.
This is an expand migration: the column is nullable so existing message
rows stay valid with NULL; no backfill is needed before the new path
starts writing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7f8e9a0b1c2d"
down_revision = "1c2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        schema="map_control",
    )
    op.create_foreign_key(
        "fk_messages_run_id_runs",
        "messages",
        "runs",
        ["run_id"],
        ["id"],
        source_schema="map_control",
        referent_schema="map_control",
        ondelete="SET NULL",
    )
    op.create_index("ix_messages_run_id", "messages", ["run_id"], schema="map_control")


def downgrade() -> None:
    op.drop_index("ix_messages_run_id", table_name="messages", schema="map_control")
    op.drop_constraint(
        "fk_messages_run_id_runs", "messages", schema="map_control", type_="foreignkey"
    )
    op.drop_column("messages", "run_id", schema="map_control")
