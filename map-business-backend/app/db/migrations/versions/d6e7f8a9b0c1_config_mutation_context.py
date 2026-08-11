"""config mutation crash context (R3-P1-01)

``config_mutations`` used to persist only ``expected_hash`` before the
atomic rename; ``target_hash`` and the audit event were committed only
AFTER the rename. A crash in that window therefore left a pending row
with ``target_hash = NULL`` that the reconciler could not distinguish
from an unrelated write — any hash drift was mis-attributed as
``applied``.

The mutation is now split into prepare + apply:

1. prepare computes the target state/hash purely in memory (no write);
2. the pending row is committed BEFORE the rename, carrying
   ``expected_hash`` + ``target_hash`` plus the original request context
   (workspace / actor / request id / action);
3. the expected-hash CAS + atomic rename happens only afterwards;
4. the reconciler then knows exactly which hashes can legitimately close
   a pending row (``current == expected`` -> no write; ``current ==
   target`` -> applied; anything else -> ``UNKNOWN_STATE``, never a
   guessed ``applied``) and can attribute the recovered audit event to
   the original actor instead of a synthetic identity.

All new columns are nullable: rows created before this migration keep
reconciling through the conservative ``UNKNOWN_STATE`` path.
"""

import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "config_mutations",
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "config_mutations",
        sa.Column("action", sa.String(length=64), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "config_mutations",
        sa.Column("actor_user_id", sa.String(length=192), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "config_mutations",
        sa.Column("actor_subject", sa.String(length=192), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "config_mutations",
        sa.Column("actor_roles", sa.dialects.postgresql.JSONB(), nullable=True),
        schema="map_control",
    )
    op.add_column(
        "config_mutations",
        sa.Column("request_id", sa.String(length=128), nullable=True),
        schema="map_control",
    )


def downgrade() -> None:
    for column in (
        "request_id",
        "actor_roles",
        "actor_subject",
        "actor_user_id",
        "action",
        "workspace_id",
    ):
        op.drop_column("config_mutations", column, schema="map_control")
