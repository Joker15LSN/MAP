"""effect ledger dispatch fence (R4-P0-01)

Adds the concurrency fence columns to ``map_control.effect_ledger`` so a
``dispatching`` row carries the identity of the dispatcher and a lease:

- ``dispatch_owner`` / ``dispatch_attempt``: the job worker identity that
  committed the ``pending -> dispatching`` transition (or re-adopted it
  during recovery);
- ``dispatch_expires_at``: database-time deadline bounding the dispatch.

Recovery semantics with these columns (together with the provider-side
idempotency contract of R4-P0-01):

- a second LIVE caller observing ``dispatching`` never marks the row
  ``uncertain`` while the dispatch lease is alive — the first dispatcher
  may still be in flight;
- after the lease expires an unresolved ``dispatching`` row is resolved by
  querying the provider by idempotency key: confirmed -> ``delivered``,
  clearly never received -> re-adopt and re-send with the SAME key,
  unknown -> ``uncertain`` (fail closed).
"""

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "effect_ledger",
        sa.Column("dispatch_owner", sa.String(length=128), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "effect_ledger",
        sa.Column("dispatch_attempt", sa.Integer(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "effect_ledger",
        sa.Column("dispatch_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_column("effect_ledger", "dispatch_expires_at", schema="map_control")
    op.drop_column("effect_ledger", "dispatch_attempt", schema="map_control")
    op.drop_column("effect_ledger", "dispatch_owner", schema="map_control")
