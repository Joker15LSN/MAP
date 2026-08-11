"""audit append-only grants (R2-P1-04)

Enforces the audit privilege contract in the database itself instead of by
convention: the application role keeps DML on regular tables (via default
privileges) but on the audit tables gets exactly

- ``config_audit_events``          SELECT + INSERT only (append-only)
- ``config_audit_events_quarantine`` SELECT only (operator-run repair)
- ``config_audit_chain_head``      SELECT + INSERT + UPDATE (append point;
  no DELETE)
- ``config_mutations``             full DML (mutable orchestration table)
- ``alembic_version``              SELECT (readiness probe)

The role name is the contract-fixed application role ``map``; environments
without that role skip the block (guarded), so the migration stays runnable
everywhere.

Revision ID: 9a2b3c4d5e6f
Revises: 8d1e2f3a4b5c
Create Date: 2026-08-09 20:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "9a2b3c4d5e6f"
down_revision = "8d1e2f3a4b5c"
branch_labels = None
depends_on = None

_APP_ROLE = "map"

# table -> (privileges revoked from the app role, privileges granted to it)
_TABLE_PRIVS = {
    "config_audit_events": ("UPDATE, DELETE, TRUNCATE", "SELECT, INSERT"),
    "config_audit_events_quarantine": ("INSERT, UPDATE, DELETE, TRUNCATE", "SELECT"),
    "config_audit_chain_head": ("DELETE, TRUNCATE", "SELECT, INSERT, UPDATE"),
}


def _grant_block() -> str:
    lines = []
    for table, (revoked, granted) in _TABLE_PRIVS.items():
        lines.append(f"EXECUTE 'REVOKE {revoked} ON map_control.{table} FROM {_APP_ROLE}';")
        lines.append(f"EXECUTE 'GRANT {granted} ON map_control.{table} TO {_APP_ROLE}';")
    lines.append(
        f"EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON map_control.config_mutations TO {_APP_ROLE}';"
    )
    lines.append(f"EXECUTE 'GRANT SELECT ON map_control.alembic_version TO {_APP_ROLE}';")
    return "\n        ".join(lines)


def _restore_block() -> str:
    lines = [
        f"EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON map_control.{table} TO {_APP_ROLE}';"
        for table in _TABLE_PRIVS
    ]
    return "\n        ".join(lines)


def upgrade() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        {_grant_block()}
    END IF;
END
$$;
"""
    )


def downgrade() -> None:
    op.execute(
        f"""
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        {_restore_block()}
    END IF;
END
$$;
"""
    )
