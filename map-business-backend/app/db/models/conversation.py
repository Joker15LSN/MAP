"""Conversation / Message / MessageEvidence models (R1-CONV-01).

A user message and its assistant reply share one ``request_id`` (unique);
retrying the same request_id must not create a second assistant message.
Messages checkpoint content deltas into the DB while streaming so a BFF
crash leaves at most a ``streaming`` assistant message (reconciled later).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
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


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="global")
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="新会话")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # A request_id maps to exactly one assistant message (idempotency
        # contract); the paired user message shares the same request_id.
        Index(
            "uq_messages_assistant_request_id",
            "request_id",
            unique=True,
            postgresql_where=text("role = 'assistant' AND request_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("map_control.conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="completed", index=True
    )  # streaming | completed | stopped | failed
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    config_snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # FIX-P1-CONV-01: stable stream facts (fallback must not erase the
    # original error). stream_error is a stable error code, e.g.
    # STREAM_EOF_WITHOUT_DONE / STREAM_PARSE_ERROR / STREAM_INTERRUPTED.
    stream_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class MessageEvidence(Base):
    __tablename__ = "message_evidence"
    __table_args__ = (
        UniqueConstraint("message_id", "ordinal", name="uq_message_evidence_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("map_control.messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
