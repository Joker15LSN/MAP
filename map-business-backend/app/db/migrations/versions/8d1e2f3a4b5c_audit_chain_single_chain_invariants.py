"""audit chain single-chain invariants (R2-P1-03)

Makes the audit hash chain fork-proof at the database level instead of
relying on the application "lock the last row" pattern:

- ``ordinal`` (UNIQUE) gives every event a total order independent of
  wall-clock ``created_at``;
- ``prev_entry_hash`` becomes NOT NULL (genesis = '') and UNIQUE, so no
  predecessor can ever have two children and there can be only one
  genesis;
- ``error_message`` is persisted so every hash-canonical field defined by
  ``audit_record_payload`` is stored — writer, verifier and reconciler now
  share one schema;
- ``config_audit_chain_head`` is the single serialized append point:
  writers lock the head row (``SELECT ... FOR UPDATE``) and update it in
  the same transaction as the event insert;
- ``config_audit_events_quarantine`` stores broken-suffix rows moved out
  by an operator (detection/quarantine only — history is never silently
  rewritten).

Revision ID: 8d1e2f3a4b5c
Revises: 7c0b1d3e5f6a
Create Date: 2026-08-09 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8d1e2f3a4b5c"
down_revision = "7c0b1d3e5f6a"
branch_labels = None
depends_on = None

_AUDIT_COLUMNS = [
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
    sa.Column("prev_entry_hash", sa.String(length=64), nullable=False),
    sa.Column("entry_hash", sa.String(length=64), nullable=False),
    sa.Column("ordinal", sa.BigInteger(), nullable=False),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
]


def upgrade() -> None:
    # --- single-chain invariants on config_audit_events -------------------
    op.add_column(
        "config_audit_events", sa.Column("ordinal", sa.BigInteger()), schema="map_control"
    )
    op.add_column(
        "config_audit_events", sa.Column("error_message", sa.Text()), schema="map_control"
    )
    # Backfill legacy rows: the legacy writer hashed with error_message=None
    # (recovery bad-file events are the exception and will be DETECTED as
    # broken by the verifier — they are never silently rewritten).
    op.execute(
        "UPDATE map_control.config_audit_events e SET ordinal = s.ordinal FROM ("
        "  SELECT id, row_number() OVER (ORDER BY created_at, id) - 1 AS ordinal"
        "  FROM map_control.config_audit_events"
        ") s WHERE e.id = s.id"
    )
    op.execute(
        "UPDATE map_control.config_audit_events SET prev_entry_hash = '' "
        "WHERE prev_entry_hash IS NULL"
    )
    op.alter_column("config_audit_events", "ordinal", nullable=False, schema="map_control")
    op.alter_column(
        "config_audit_events", "prev_entry_hash", nullable=False, schema="map_control"
    )
    op.create_unique_constraint(
        "uq_audit_events_ordinal", "config_audit_events", ["ordinal"], schema="map_control"
    )
    op.create_unique_constraint(
        "uq_audit_events_prev_entry_hash",
        "config_audit_events",
        ["prev_entry_hash"],
        schema="map_control",
    )
    op.create_check_constraint(
        "ck_audit_events_ordinal_nonneg",
        "config_audit_events",
        "ordinal >= 0",
        schema="map_control",
    )

    # --- serialized append point -------------------------------------------
    op.create_table(
        "config_audit_chain_head",
        sa.Column("chain_id", sa.SmallInteger(), nullable=False),
        sa.Column("head_ordinal", sa.BigInteger(), nullable=False),
        sa.Column("head_entry_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("chain_id"),
        schema="map_control",
    )
    # NOTE: the count/hash MUST come FROM config_audit_events. The original
    # statement omitted the FROM clause, and PostgreSQL evaluates a bare
    # ``count(*)`` over the implicit single empty row -> 1, which seeded
    # head_ordinal=1 on an EMPTY table and broke the chain verifier (the
    # first real event got ordinal 1 while the verifier walks from 0).
    # Found by the R2-P1-05 Compose E2E; existing databases are repaired
    # by the follow-up migration b3c4d5e6f7a8.
    op.execute(
        "INSERT INTO map_control.config_audit_chain_head "
        "(chain_id, head_ordinal, head_entry_hash) "
        "SELECT 1, count(*), COALESCE("
        "  (SELECT entry_hash FROM map_control.config_audit_events"
        "   ORDER BY ordinal DESC LIMIT 1), '') "
        "FROM map_control.config_audit_events"
    )

    # --- quarantine table (operator-run repair, history preserved) ---------
    op.create_table(
        "config_audit_events_quarantine",
        sa.Column(
            "original_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        *_AUDIT_COLUMNS,
        sa.Column(
            "quarantined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("original_id"),
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_table("config_audit_events_quarantine", schema="map_control")
    op.drop_table("config_audit_chain_head", schema="map_control")
    op.drop_constraint(
        "ck_audit_events_ordinal_nonneg", "config_audit_events", schema="map_control"
    )
    op.drop_constraint(
        "uq_audit_events_prev_entry_hash", "config_audit_events", schema="map_control"
    )
    op.drop_constraint("uq_audit_events_ordinal", "config_audit_events", schema="map_control")
    op.alter_column(
        "config_audit_events", "prev_entry_hash", nullable=True, schema="map_control"
    )
    op.drop_column("config_audit_events", "error_message", schema="map_control")
    op.drop_column("config_audit_events", "ordinal", schema="map_control")
