"""sandbox invocation ledger (S4-01): OpenSandbox exactly-once facts.

The map_core OpenSandbox tool keeps a durable invocation ledger so that the
same (workspace_id, invocation_id) is claimed atomically and a retry never
re-issues a remote side effect. The ledger is the source of truth once the
remote sandbox is destroyed (and therefore no longer queryable).

UNIQUE(workspace_id, invocation_id) is the claim boundary; the request_digest
captures the normalized command + resource limits so a different payload for
the same id conflicts instead of replaying an old result. Grants follow the
regular-table default privileges (full DML for the app role) - this is an
orchestration table like effect_ledger, not append-only audit history.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandbox_invocations",
        # map_core identity ids are validated strings (e.g. ws-1, run-1), not
        # UUIDs, so these stay TEXT to match the raw-asyncpg contract.
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("create_key", sa.Text(), nullable=False),
        sa.Column("execute_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sandbox_id", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("server_state", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "workspace_id", "invocation_id", name="pk_sandbox_invocations"
        ),
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_table("sandbox_invocations", schema="map_control")
