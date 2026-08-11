"""message stream facts (FIX-P1-CONV-01)

Adds stable stream-fact columns to messages: stream_error (stable error
code), error_message, fallback_used. Expand-only migration.

Revision ID: 5e7f9c1d3a2b
Revises: 4c9e1f2a8b3d
Create Date: 2026-08-09 02:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "5e7f9c1d3a2b"
down_revision = "4c9e1f2a8b3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("stream_error", sa.String(length=64), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "messages",
        sa.Column("error_message", sa.Text(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "messages",
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema="map_control",
    )
    op.add_column(
        "messages",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_column("messages", "updated_at", schema="map_control")
    op.drop_column("messages", "fallback_used", schema="map_control")
    op.drop_column("messages", "error_message", schema="map_control")
    op.drop_column("messages", "stream_error", schema="map_control")
