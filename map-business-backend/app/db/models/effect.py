"""Effect ledger model (R3-P0-01).

Persisted fact source for external side effects of job handlers. One row
per ``(workspace_id, effect_key)`` tracks the effect through
``pending -> dispatching -> delivered | uncertain`` so every crash window
is observable and the effect can never be blindly replayed (see migration
``c5d6e7f8a9b0`` for the full protocol).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

EFFECT_PENDING = "pending"
EFFECT_DISPATCHING = "dispatching"
EFFECT_DELIVERED = "delivered"
EFFECT_UNCERTAIN = "uncertain"


class EffectLedger(Base):
    __tablename__ = "effect_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    effect_key: Mapped[str] = mapped_column(String(256), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=EFFECT_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # R4-P0-01 / R5-P0-01 concurrency fence. ``dispatch_token`` is a
    # NON-REUSABLE fencing token minted for every dispatch generation
    # (``pending -> dispatching`` and every CAS takeover); together with
    # ``dispatch_owner`` / ``dispatch_attempt`` it forms the compare-and-set
    # predicate of EVERY owner-sensitive UPDATE, so a stale worker can never
    # overwrite the generation that superseded it (rowcount=0 instead).
    # ``dispatch_expires_at`` is the database-time deadline bounding the
    # dispatch: a takeover is only allowed once it has passed. Rows written
    # before this migration carry NULL in all four columns and are treated
    # as an expired NULL generation (matched with IS NOT DISTINCT FROM).
    dispatch_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    dispatch_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dispatch_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now()
    )
