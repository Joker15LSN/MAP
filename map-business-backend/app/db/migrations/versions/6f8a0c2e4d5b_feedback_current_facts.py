"""feedback current-fact model (FIX-P1-FEEDBACK-01)

Expands message_feedback with the planned current-fact fields
(user_id, conversation_id, request_id, rating, reason_codes, reason_other,
correction_text, status, version, withdrawn_at), adds the partial unique
index (message_id, user_id) for active rows, and backfills legacy
kind/reason rows into rating fields (idempotent; legacy rows keep
user_id NULL so they do not collide).

Revision ID: 6f8a0c2e4d5b
Revises: 5e7f9c1d3a2b
Create Date: 2026-08-09 03:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "6f8a0c2e4d5b"
down_revision = "5e7f9c1d3a2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_feedback",
        sa.Column("conversation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("request_id", sa.String(length=128), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("user_id", sa.String(length=128), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("rating", sa.String(length=16), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("reason_codes", sa.dialects.postgresql.JSONB(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("reason_other", sa.Text(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("correction_text", sa.Text(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        schema="map_control",
    )
    op.add_column(
        "message_feedback",
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        schema="map_control",
    )

    # Legacy columns become nullable so new current-fact writes (no kind/
    # reason) are allowed; old rows keep their values (read compatibility).
    op.alter_column(
        "message_feedback",
        "kind",
        existing_type=sa.String(length=16),
        nullable=True,
        schema="map_control",
    )
    op.alter_column(
        "message_feedback", "reason", existing_type=sa.Text(), nullable=True, schema="map_control"
    )

    # Idempotent backfill of legacy kind/reason rows into rating fields.
    # Only fills NULL rating columns; re-running is a no-op.
    op.execute(
        sa.text(
            "UPDATE map_control.message_feedback SET "
            "rating = CASE kind WHEN 'thumbs_up' THEN 'helpful' "
            "WHEN 'thumbs_down' THEN 'unhelpful' END, "
            "reason_other = reason "
            "WHERE rating IS NULL AND kind IN ('thumbs_up', 'thumbs_down')"
        )
    )

    op.create_index(
        "uq_feedback_active_message_user",
        "message_feedback",
        ["message_id", "user_id"],
        schema="map_control",
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND status <> 'withdrawn'"),
    )
    op.create_index(
        "ix_feedback_workspace_created",
        "message_feedback",
        ["workspace_id", "created_at"],
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feedback_workspace_created", table_name="message_feedback", schema="map_control"
    )
    op.drop_index(
        "uq_feedback_active_message_user", table_name="message_feedback", schema="map_control"
    )
    op.drop_column("message_feedback", "withdrawn_at", schema="map_control")
    op.drop_column("message_feedback", "version", schema="map_control")
    op.drop_column("message_feedback", "status", schema="map_control")
    op.drop_column("message_feedback", "correction_text", schema="map_control")
    op.drop_column("message_feedback", "reason_other", schema="map_control")
    op.drop_column("message_feedback", "reason_codes", schema="map_control")
    op.drop_column("message_feedback", "rating", schema="map_control")
    op.drop_column("message_feedback", "user_id", schema="map_control")
    op.drop_column("message_feedback", "request_id", schema="map_control")
    op.drop_column("message_feedback", "conversation_id", schema="map_control")
