"""Stale streaming message reconciler (FIX-P1-CONV-01).

After a BFF crash, a message can stay ``streaming`` forever. The reconciler
marks such messages ``failed`` with the stable error code
``STREAM_INTERRUPTED``. Idempotent: the conditional UPDATE only touches
rows still in ``streaming``, so it can never overwrite a terminal state
written concurrently.

Registered as a worker handler for ``job_type="message_reconcile"``
(see app/workers/main.py); operators enqueue one job per interval, or the
function can be called directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db.models import Message
from .conversation_service import STREAM_INTERRUPTED

logger = logging.getLogger(__name__)

DEFAULT_STALE_AFTER_S = 300


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def reconcile_stale_streaming_messages(
    session_factory: async_sessionmaker,
    *,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    limit: int = 200,
) -> int:
    """Mark streaming messages older than the threshold as failed/interrupted.

    Returns the number of messages reconciled. Observability: logs the
    reconciled message ids.
    """
    now = _utcnow()
    threshold = now - timedelta(seconds=stale_after_s)
    stale_ids = (
        select(Message.id)
        .where(Message.status == "streaming", Message.updated_at < threshold)
        .order_by(Message.updated_at.asc())
        .limit(limit)
    )
    async with session_factory() as session:
        result = await session.execute(
            update(Message)
            .where(Message.id.in_(stale_ids))
            .values(
                status="failed",
                stream_error=STREAM_INTERRUPTED,
                error_message="message interrupted by reconciler after BFF restart",
                completed_at=now,
                version=Message.version + 1,
            )
            .returning(Message.id)
        )
        ids = result.scalars().all()
        await session.commit()
    if ids:
        logger.warning(
            "reconciled stale streaming messages",
            extra={"count": len(ids), "ids": [str(i) for i in ids]},
        )
    return len(ids)
