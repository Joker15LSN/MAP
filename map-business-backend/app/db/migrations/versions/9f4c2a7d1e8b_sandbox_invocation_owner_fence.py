"""sandbox invocation ownership fencing (S5-01): crash-safe takeover.

S4-01 made the claim atomic but left non-terminal rows (pending/created)
without an owner, lease, fencing token or attempt counter: a process crash
between the remote create/execute and the ledger write left the row
permanently non-terminal, and a concurrent caller could only wait.

This migration adds the R5-P1-SANDBOX fence columns so map_core can:

- record WHO owns the row (owner_id) and until when (lease_expires_at);
- fence every owner-sensitive write (record_created/complete) with a
  non-reusable fencing_token + attempt, so a superseded owner observes
  rowcount 0 instead of overwriting the current generation;
- take over EXPIRED non-terminal rows with a single CAS UPDATE (a new
  token + attempt bump), so the durable reconciler / a retry can converge
  crashed invocations to a definite terminal state;
- scan expired rows efficiently via a partial index on
  (status, lease_expires_at).

NULL semantics (pre-migration rows): a NULL fencing_token matches
IS NOT DISTINCT FROM (a valid observed generation) and a NULL lease counts
as ALREADY EXPIRED, so rows created before this migration stay
takeover-able instead of being stranded.

Grants: the app role keeps full DML on this orchestration table (regular
table default privileges from the S4-01 migration) - map_core performs DML
only, as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "9f4c2a7d1e8b"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sandbox_invocations",
        sa.Column("owner_id", sa.Text(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "sandbox_invocations",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "sandbox_invocations",
        sa.Column("fencing_token", sa.Text(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "sandbox_invocations",
        sa.Column(
            "attempt",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        schema="map_control",
    )
    # The durable reconciler re-drives a crashed invocation with the SAME
    # idempotency keys, so the original normalized request (command +
    # resource limits) must be part of the row - it is the only place the
    # original command survives an owner crash before record_created.
    op.add_column(
        "sandbox_invocations",
        sa.Column(
            "request_payload",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        schema="map_control",
    )
    op.add_column(
        "sandbox_invocations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="map_control",
    )
    op.create_index(
        "ix_sandbox_invocations_nonterminal_lease",
        "sandbox_invocations",
        ["status", "lease_expires_at"],
        unique=False,
        schema="map_control",
        postgresql_where=sa.text(
            "status IN ('pending', 'created')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sandbox_invocations_nonterminal_lease",
        table_name="sandbox_invocations",
        schema="map_control",
    )
    op.drop_column("sandbox_invocations", "updated_at", schema="map_control")
    op.drop_column("sandbox_invocations", "request_payload", schema="map_control")
    op.drop_column("sandbox_invocations", "attempt", schema="map_control")
    op.drop_column("sandbox_invocations", "fencing_token", schema="map_control")
    op.drop_column("sandbox_invocations", "lease_expires_at", schema="map_control")
    op.drop_column("sandbox_invocations", "owner_id", schema="map_control")
