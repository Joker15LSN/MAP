"""effect ledger (R3-P0-01): provable at-most-once external effects

Replaces the old ``EffectGuard`` at-most-once CLAIM (a committed
idempotency record written BEFORE the external call) with a persisted
effect ledger that makes every crash window observable::

    pending -> dispatching -> delivered
                           or -> uncertain   (terminal, observable)

State machine and crash windows (R3-P0-01 acceptance):

- ``pending``       intent recorded; the external call may not have
                    happened yet — a retry proceeds with the call;
- ``dispatching``   the external call was started; a crash in this state
                    means the outcome is UNKNOWN, so recovery marks the
                    effect ``uncertain`` instead of replaying it (this is
                    the window the old claim-based guard could not
                    distinguish, silently reporting success with zero
                    external actions);
- ``delivered``     the provider confirmed the effect; retries skip it;
- ``uncertain``     terminal: the effect may or may not have happened and
                    can never be retried blindly. Jobs attached to such an
                    effect fail with ``EFFECT_UNCERTAIN`` (never fake
                    ``succeeded``).

``UNIQUE(workspace_id, effect_key)`` makes the key the single fact source
across processes, retries, kills and lease takeovers. Grants follow the
regular-table default privileges (full DML for the app role) — the ledger
is an orchestration table like ``config_mutations``, not append-only audit
history.
"""

import sqlalchemy as sa
from alembic import op

revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "effect_ledger",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effect_key", sa.String(length=256), nullable=False),
        sa.Column("job_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            sa.CheckConstraint(
                "status IN ('pending', 'dispatching', 'delivered', 'uncertain')",
                name="ck_effect_ledger_status",
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "effect_key", name="uq_effect_ledger_ws_key"),
        schema="map_control",
    )
    op.create_index(
        "ix_effect_ledger_status", "effect_ledger", ["status"], schema="map_control"
    )


def downgrade() -> None:
    op.drop_index("ix_effect_ledger_status", table_name="effect_ledger", schema="map_control")
    op.drop_table("effect_ledger", schema="map_control")
