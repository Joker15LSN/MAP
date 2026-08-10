"""repair audit chain head seed (R2-P1-05 E2E finding)

Migration ``8d1e2f3a4b5c`` seeded ``config_audit_chain_head`` with
``SELECT 1, count(*), ...`` but WITHOUT a FROM clause. PostgreSQL
evaluates a bare ``count(*)`` over the implicit single empty row and
returns 1, so every fresh database was seeded with ``head_ordinal = 1``
while ``config_audit_events`` was empty. The first real audit event then
got ``ordinal = 1`` and ``/api/v1/admin/audit-events/verify`` (which
walks ordinals starting at 0) reported the chain broken at index 0.

Repair: recompute the head from the events table itself. The statement
is idempotent and a no-op on healthy chains (count and last entry hash
already match), so it is safe for both fresh and long-lived databases.

The Compose E2E (``e2e/run_e2e.py``) caught this because it verifies the
chain on a REAL fresh volume; integration tests truncate every table
between cases and therefore re-seeded the head correctly via the writer.
"""

from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "9a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE map_control.config_audit_chain_head h SET "
        "head_ordinal = (SELECT count(*) FROM map_control.config_audit_events), "
        "head_entry_hash = COALESCE("
        "  (SELECT entry_hash FROM map_control.config_audit_events"
        "   ORDER BY ordinal DESC LIMIT 1), ''), "
        "updated_at = now() "
        "WHERE h.chain_id = 1"
    )


def downgrade() -> None:
    # The repair only corrects stored values; there is nothing to revert.
    pass
