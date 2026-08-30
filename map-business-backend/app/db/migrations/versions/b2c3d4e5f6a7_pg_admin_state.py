"""PG single-row admin state (Step 7 PR-J7a)

Adds the singleton ``map_control.admin_state`` table that becomes the
durable home of AdminState. Exactly one row (``id = 1``), enforced by the
primary key default and a CHECK constraint. ``state_json`` holds the full
validated AdminState document; ``state_hash`` holds the canonical SHA-256
digest of ``state_json`` so a reader can detect tampering and fail closed.

Grants follow the fail-closed privilege contract: the app role gets
SELECT + UPDATE only (no DELETE, no DDL); rows are written/verified by
``app.services.runtime_snapshot.adapters.admin_state_pg``.

Revision ID: b2c3d4e5f6a7
Revises: a7b8c9d0e1f2
Create Date: 2026-08-30 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2c3d4e5f6a7"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

_APP_ROLE = "map"


def _grant_block() -> str:
    return (
        f"EXECUTE 'GRANT SELECT, UPDATE ON map_control.admin_state TO {_APP_ROLE}';"
    )


def _revoke_block() -> str:
    return f"EXECUTE 'REVOKE ALL ON map_control.admin_state FROM {_APP_ROLE}';"


def upgrade() -> None:
    op.create_table(
        "admin_state",
        sa.Column(
            "id",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("state_json", postgresql.JSONB(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_admin_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
        schema="map_control",
    )
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
        {_revoke_block()}
    END IF;
END
$$;
"""
    )
    op.drop_table("admin_state", schema="map_control")
