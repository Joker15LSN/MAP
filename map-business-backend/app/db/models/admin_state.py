"""Singleton PG-backed AdminState row (Step 7 PR-J7a).

Exactly one row (``id = 1``); ``state_hash`` is the canonical SHA-256 of
``state_json`` (same algorithm as ``app.services.runtime_snapshot.digest``)
so readers fail closed on tampering.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class AdminStateRow(Base):
    """The durable admin state document (single row, fail-closed reads)."""

    __tablename__ = "admin_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_admin_state_singleton"),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, server_default=text("1")
    )
    state_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
