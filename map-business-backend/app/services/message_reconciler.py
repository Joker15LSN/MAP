"""Stale streaming message reconciler (FIX-P1-CONV-01 / R3-P0-01).

After a BFF crash, a message can stay ``streaming`` forever. The reconciler
marks such messages ``failed`` with the stable error code
``STREAM_INTERRUPTED``. Idempotent: the conditional UPDATE only touches
rows still in ``streaming``, so it can never overwrite a terminal state
written concurrently.

Registered as a worker handler for ``job_type="message_reconcile"``
(see app/workers/main.py); operators enqueue one job per interval.

R3-P0-01 transaction discipline: the function executes on the CALLER's
session and only flushes — it never opens its own session and never
commits. Inside the worker the runner's fenced ``complete()`` commits the
business writes and ``Job.status=succeeded`` in ONE transaction; when the
lease is lost the fenced UPDATE misses and the runner rolls back, so a
stale worker can never persist message writes. The lease is re-checked
immediately before the UPDATE.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Message
from .conversation_service import STREAM_INTERRUPTED

logger = logging.getLogger(__name__)

DEFAULT_STALE_AFTER_S = 300


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def reconcile_stale_streaming_messages(
    session: AsyncSession,
    *,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    limit: int = 200,
) -> int:
    """Mark streaming messages older than the threshold as failed/interrupted.

    Runs on the caller's session and FLUSHES only; the caller owns the
    commit (worker: the fenced job completion). Returns the number of
    messages reconciled and logs the reconciled ids (observability).
    """
    # R3-P0-01: a handler that already lost its lease must not produce
    # further writes; the runner rolls back anything uncommitted anyway,
    # but fail closed at the safe point instead of relying on rollback.
    from ..workers.job_runner import get_current_job_context

    ctx = get_current_job_context()
    if ctx is not None and not ctx.lease_ok:
        logger.warning(
            "message reconcile skipped: lease lost",
            extra={"job_id": str(ctx.job_id), "worker_id": ctx.worker_id},
        )
        return 0

    now = _utcnow()
    threshold = now - timedelta(seconds=stale_after_s)
    stale_ids = (
        select(Message.id)
        .where(Message.status == "streaming", Message.updated_at < threshold)
        .order_by(Message.updated_at.asc())
        .limit(limit)
    )
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
    await session.flush()
    if ids:
        logger.warning(
            "reconciled stale streaming messages",
            extra={"count": len(ids), "ids": [str(i) for i in ids]},
        )
    return len(ids)
