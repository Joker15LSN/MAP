"""Runtime snapshot durable models (Step 7 PR-J).

``runtime_snapshots`` is immutable except for ``status`` transitions
(enforced by a DB trigger); ``runtime_snapshot_current`` is the singleton
current pointer (at most one active snapshot globally, also enforced by a
partial unique index); ``runtime_snapshot_mutations`` is a mutable
orchestration table used for crash recovery (it is NOT part of the audit
chain).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

SNAPSHOT_STATUSES = ("draft", "published", "active", "rolled_back", "retired")
MUTATION_STATUSES = ("pending", "applied", "failed")


class RuntimeSnapshot(Base):
    """Immutable projection snapshot.

    Immutability contract (PR-J): after insert only ``status`` may
    change; every other column is protected by
    ``runtime_snapshots_guard_update``.
    """

    __tablename__ = "runtime_snapshots"
    __table_args__ = (
        UniqueConstraint("digest", name="uq_runtime_snapshots_digest"),
        CheckConstraint(
            "status IN ('draft', 'published', 'active', 'rolled_back', 'retired')",
            name="ck_runtime_snapshots_status",
        ),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_runtime_snapshots_schema_version_positive",
        ),
        Index(
            "uq_runtime_snapshots_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        ForeignKeyConstraint(
            ["parent_id"],
            ["map_control.runtime_snapshots.id"],
            name="fk_runtime_snapshots_parent_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    projection: Mapped[dict] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuntimeSnapshotCurrent(Base):
    """Singleton current pointer (id = 1)."""

    __tablename__ = "runtime_snapshot_current"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_runtime_snapshot_current_singleton"),
        ForeignKeyConstraint(
            ["current_snapshot_id"],
            ["map_control.runtime_snapshots.id"],
            name="fk_runtime_snapshot_current_snapshot_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    current_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuntimeSnapshotMutation(Base):
    """Crash-recovery orchestration row (NOT part of the audit chain).

    Mirrors ``config_mutations``: the row is committed BEFORE the file
    rename and carries both admin hashes, the target snapshot id/digest/
    projection and the original request context, so the reconciler can
    close a pending row only on exact hash/pointer matches.
    """

    __tablename__ = "runtime_snapshot_mutations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_runtime_snapshot_mutations_status",
        ),
        Index("ix_runtime_snapshot_mutations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    resource: Mapped[str] = mapped_column(String(192), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expected_admin_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_admin_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_current_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_current_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_projection: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(192), nullable=True)
    actor_subject: Mapped[str | None] = mapped_column(String(192), nullable=True)
    actor_roles: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
