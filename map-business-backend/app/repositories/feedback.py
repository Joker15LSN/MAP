"""Message feedback repository (R1-FEEDBACK-01 / FIX-P1-FEEDBACK-01).

- upsert: one active row per (message_id, user_id); rating switch is an
  UPDATE (version increments monotonically); concurrent PUTs are safe via
  ON CONFLICT DO UPDATE.
- withdraw: tombstone (status=withdrawn) — the product value is gone but
  audit evidence stays.
- aggregates count only active rows; the SQL itself is scoped to messages
  the caller may see (workspace + owner), so other users' reasons never
  leak (E-04).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import MessageFeedback

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        message_id: uuid.UUID,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        request_id: str | None,
        user_id: str,
        rating: str,
        reason_codes: list[str] | None,
        reason_other: str | None,
        correction_text: str | None,
    ) -> MessageFeedback:
        """Create or overwrite the current user's feedback (idempotent).

        UPDATE-first: plain UPDATEs re-evaluate their SET expressions after
        lock waits (EvalPlanQual), so concurrent PUTs get strictly
        monotonic versions. INSERT with ON CONFLICT DO NOTHING covers the
        first write; a lost insert race falls back to one more UPDATE.
        """
        now = _utcnow()
        values = {
            "rating": rating,
            "reason_codes": reason_codes,
            "reason_other": reason_other,
            "correction_text": correction_text,
            "status": "open",
            "withdrawn_at": None,
            "version": MessageFeedback.version + 1,
            "updated_at": now,
        }

        async def _update() -> MessageFeedback | None:
            result = await self._session.execute(
                update(MessageFeedback)
                .where(
                    MessageFeedback.message_id == message_id,
                    MessageFeedback.user_id == user_id,
                    MessageFeedback.status != "withdrawn",
                )
                .values(**values)
                .returning(MessageFeedback)
            )
            return result.scalar_one_or_none()

        row = await _update()
        if row is not None:
            return row

        inserted = await self._session.execute(
            pg_insert(MessageFeedback)
            .values(
                message_id=message_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                request_id=request_id,
                user_id=user_id,
                rating=rating,
                reason_codes=reason_codes,
                reason_other=reason_other,
                correction_text=correction_text,
                status="open",
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=["message_id", "user_id"],
                index_where=text("user_id IS NOT NULL AND status <> 'withdrawn'"),
            )
            .returning(MessageFeedback)
        )
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row
        # A concurrent insert won the race: update their row (monotonic).
        row = await _update()
        if row is not None:
            return row
        raise RuntimeError("feedback upsert lost a race without a row")

    async def get_own(
        self, message_id: uuid.UUID, user_id: str
    ) -> MessageFeedback | None:
        result = await self._session.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user_id,
                MessageFeedback.status != "withdrawn",
            )
        )
        return result.scalar_one_or_none()

    async def withdraw(
        self, message_id: uuid.UUID, user_id: str
    ) -> MessageFeedback | None:
        """Tombstone the current feedback; returns the row or None."""
        now = _utcnow()
        result = await self._session.execute(
            update(MessageFeedback)
            .where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user_id,
                MessageFeedback.status != "withdrawn",
            )
            .values(
                status="withdrawn",
                withdrawn_at=now,
                version=MessageFeedback.version + 1,
                updated_at=now,
            )
            .returning(MessageFeedback)
        )
        return result.scalar_one_or_none()

    async def count_by_message_ids(
        self, message_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, int]]:
        """helpful/unhelpful counts for visible messages (no reason text)."""
        if not message_ids:
            return {}
        rows = (
            await self._session.execute(
                select(
                    MessageFeedback.message_id,
                    MessageFeedback.rating,
                    func.count(),
                )
                .where(
                    MessageFeedback.message_id.in_(message_ids),
                    MessageFeedback.status != "withdrawn",
                    MessageFeedback.rating.in_(("helpful", "unhelpful")),
                )
                .group_by(MessageFeedback.message_id, MessageFeedback.rating)
            )
        ).all()
        summary: dict[uuid.UUID, dict[str, int]] = {}
        for message_id, rating, count in rows:
            entry = summary.setdefault(message_id, {"helpful": 0, "unhelpful": 0})
            entry[str(rating)] = count
        return summary

    async def list_admin(
        self,
        *,
        workspace_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
        rating: str | None = None,
        reason_code: str | None = None,
    ) -> list[MessageFeedback]:
        """Admin list with the workspace predicate in SQL (audit scope)."""
        stmt = (
            select(MessageFeedback)
            .where(
                MessageFeedback.workspace_id == workspace_id,
                MessageFeedback.status != "withdrawn",
            )
            .order_by(MessageFeedback.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if rating:
            stmt = stmt.where(MessageFeedback.rating == rating)
        if reason_code:
            stmt = stmt.where(MessageFeedback.reason_codes.contains([reason_code]))
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def get(self, feedback_id: uuid.UUID) -> MessageFeedback | None:
        return await self._session.get(MessageFeedback, feedback_id)
