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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now()
    )
