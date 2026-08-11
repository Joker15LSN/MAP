"""Message feedback (R1-FEEDBACK-01 / FIX-P1-FEEDBACK-01).

Current-fact model: one active feedback per ``(message_id, user_id)``.
Changing thumbs-up to thumbs-down is an UPDATE, never a second row.
Withdrawals are tombstones (status=withdrawn) so audit evidence survives.

Legacy columns (kind/reason) are kept for read compatibility with the old
API; new code writes rating/reason_codes/reason_other/correction_text.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
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

RATINGS = ("helpful", "unhelpful")
REASON_CODES = (
    "incorrect",
    "outdated",
    "no_evidence",
    "not_relevant",
    "unsafe",
    "too_verbose",
    "tool_failed",
    "other",
)
FEEDBACK_STATUSES = ("open", "converted", "dismissed", "withdrawn")


class MessageFeedback(Base):
    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "kind", name="uq_feedback_message_kind"),
        # One active feedback per (message, user); withdrawn rows (and legacy
        # rows without a user) are excluded from the uniqueness guarantee.
        Index(
            "uq_feedback_active_message_user",
            "message_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL AND status <> 'withdrawn'"),
        ),
        Index("ix_feedback_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("map_control.messages.id"), nullable=False
    )
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # New current-fact fields.
    rating: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason_codes: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reason_other: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Legacy fields (read compatibility; new writes use rating fields).
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
