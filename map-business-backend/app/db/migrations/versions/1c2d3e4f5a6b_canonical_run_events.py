"""Canonical Run durable facts (Step 2 / P1-RUN-01, PR-C minimal set).

ADR-0002 / SPEC/contracts/run.md: PostgreSQL is the durable truth for the
Run lifecycle. This PR-C migration adds the minimal fact set that a Run
Attempt needs - the run row itself and the strictly-ordered event stream.
Step/Attempt/Invocation/Checkpoint rows are added by later migrations when
they get their first production writer (expand/migrate/contract).

Ownership:
- ``runs``: BFF creates; the Run worker is the only lifecycle writer.
- ``run_events``: append-only under the same lease fencing as the run row;
  UNIQUE(run_id, seq) makes the (run_id, seq) replay contract structural.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "1c2d3e4f5a6b"
down_revision = "9f4c2a7d1e8b"
branch_labels = None
depends_on = None

# Mirrors app/runtime/state_machine.py CANONICAL_STATES["run"]; the table
# check fails closed on any state outside the frozen table.
_RUN_STATES = (
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelling",
    "cancelled",
    "timed_out",
)


def upgrade() -> None:
    op.create_table(
        "runs",
        # run_id == job_id (1:1 reuse of the existing jobs lease protocol;
        # the RunStore hides that linkage from callers).
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("command_json", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        # BFF writes a cancel COMMAND only; the worker owns every state
        # transition. These two columns are the durable command fact.
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'completed', 'failed', "
            "'cancelling', 'cancelled', 'timed_out')",
            name="ck_runs_status",
        ),
        schema="map_control",
    )
    op.create_index("ix_runs_workspace_id", "runs", ["workspace_id"], schema="map_control")

    op.create_table(
        "run_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column(
            "run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("map_control.runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("schema_minor", sa.Integer(), nullable=False),
        # Full envelope JSON as the canonical text form produced by
        # EventEnvelope.to_json() (the shared codec in run_event_stream.py
        # projects/recovers this exact shape).
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("seq >= 1", name="ck_run_events_seq_positive"),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),
        sa.Index("ix_run_events_run_id_occurred_at", "run_id", "occurred_at"),
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_table("run_events", schema="map_control")
    op.drop_index("ix_runs_workspace_id", table_name="runs", schema="map_control")
    op.drop_table("runs", schema="map_control")
