"""effect ledger dispatch fencing token (R5-P0-01)

``e7f8a9b0c1d2`` added ``dispatch_owner`` / ``dispatch_attempt`` /
``dispatch_expires_at``, but those columns were only observational: the
ledger's terminal UPDATEs still matched on ``status = 'dispatching'``
alone, so a stale worker could overwrite the generation that had already
superseded it (fifth-round P0 reproduction: ``uncertain`` written by
owner A while the row belonged to owner B).

This revision adds the missing generation identity:

- ``dispatch_token``: a NON-REUSABLE UUID minted for every dispatch
  generation — the ``pending -> dispatching`` transition and every
  compare-and-set takeover mint a fresh one.

With it, every owner-sensitive UPDATE (``ack``, ``mark_uncertain``,
takeover, provider-fact reconciliation) carries the token + owner +
attempt it observed as its compare-and-set predicate, so losing the race
is observable as ``rowcount = 0`` instead of a silent overwrite.

Old rows compatibility: rows written before this revision keep NULL in
the four fence columns. NULL is a valid *generation identity* — the guard
compares with ``IS NOT DISTINCT FROM`` and treats a NULL
``dispatch_expires_at`` as an already-expired lease, so a legacy
``dispatching`` row stays recoverable through the same CAS path.
"""

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "effect_ledger",
        sa.Column("dispatch_token", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        schema="map_control",
    )


def downgrade() -> None:
    op.drop_column("effect_ledger", "dispatch_token", schema="map_control")
