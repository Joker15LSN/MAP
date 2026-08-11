"""Non-repudiation config audit (R1-AUDIT-01 / FIX-P1-AUDIT-01).

``config_audit_events`` is append-only: applied/failed/rejected writes all
leave an event; ``entry_hash`` chains every event to the previous one so
tampering is detectable. ``config_mutations`` is a mutable orchestration
table used for crash recovery (it is NOT part of the audit chain).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

AUDIT_STATUSES = ("applied", "failed", "rejected")
MUTATION_STATUSES = ("pending", "applied", "failed")


class AuditLog(Base):
    """Legacy audit table (read compatibility; new writes go to
    ``config_audit_events``)."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConfigAuditEvent(Base):
    """Append-only, hash-chained audit event.

    Single-chain invariants enforced by the database (R2-P1-03):
    ``UNIQUE(prev_entry_hash)`` — one child per predecessor, one genesis
    (``prev_entry_hash = ''``); ``UNIQUE(ordinal)`` — total order
    independent of wall clock. Appends serialize on the
    :class:`ConfigAuditChainHead` row lock.
    """

    __tablename__ = "config_audit_events"
    __table_args__ = (
        UniqueConstraint("prev_entry_hash", name="uq_audit_events_prev_entry_hash"),
        UniqueConstraint("ordinal", name="uq_audit_events_ordinal"),
        Index("ix_audit_events_created", "created_at"),
        Index("ix_audit_events_resource", "resource_type", "resource_id"),
        Index("ix_audit_events_actor", "actor_user_id"),
        Index("ix_audit_events_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_roles: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    after_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    json_patch: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    before_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovered: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    prev_entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConfigAuditChainHead(Base):
    """Single serialized append point of the audit chain (R2-P1-03).

    Exactly one row (``chain_id = 1``); writers lock it with ``SELECT ...
    FOR UPDATE`` and advance ``head_ordinal``/``head_entry_hash`` in the
    same transaction as the event insert.
    """

    __tablename__ = "config_audit_chain_head"

    chain_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    head_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConfigMutation(Base):
    """Crash-recovery orchestration row (NOT part of the audit chain).

    R3-P1-01: the row is committed BEFORE the atomic rename and carries
    ``expected_hash`` + ``target_hash`` + the original request context, so
    the reconciler can close a pending row only on an exact hash match and
    attribute the recovered audit event to the original actor.
    """

    __tablename__ = "config_mutations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    resource: Mapped[str] = mapped_column(String(192), nullable=False)
    expected_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
