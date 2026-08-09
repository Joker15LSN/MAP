"""seed default workspace (FIX-P1-DEPLOY-01)

Idempotently insert the stable default workspace so the default Compose
configuration can create conversations without manual seeding. The id and
code are shared constants (app.settings.DEFAULT_WORKSPACE_ID / _CODE) used
by settings, API, tests and docker-compose.

Revision ID: 4c9e1f2a8b3d
Revises: 8ef8739dcb7c
Create Date: 2026-08-09 02:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "4c9e1f2a8b3d"
down_revision = "8ef8739dcb7c"
branch_labels = None
depends_on = None

DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_WORKSPACE_CODE = "default"


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO map_control.workspaces (id, code, name, status, created_at) "
            "VALUES (CAST(:wid AS uuid), :code, '默认工作空间', 'active', now()) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(
            wid=DEFAULT_WORKSPACE_ID,
            code=DEFAULT_WORKSPACE_CODE,
        )
    )
    # If a conflicting row already exists under a different id, keep the
    # stable id mapping consistent by updating that row's id only when the
    # existing row has no product data yet (defensive; seed runs on empty
    # databases in practice).
    op.execute(
        sa.text(
            "UPDATE map_control.workspaces SET id = CAST(:wid AS uuid) "
            "WHERE code = :code AND id <> CAST(:wid AS uuid) AND NOT EXISTS ("
            "  SELECT 1 FROM map_control.conversations c "
            "  WHERE c.workspace_id = map_control.workspaces.id"
            ")"
        ).bindparams(wid=DEFAULT_WORKSPACE_ID, code=DEFAULT_WORKSPACE_CODE)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM map_control.workspaces WHERE id = CAST(:wid AS uuid) AND code = :code"
        ).bindparams(wid=DEFAULT_WORKSPACE_ID, code=DEFAULT_WORKSPACE_CODE)
    )
