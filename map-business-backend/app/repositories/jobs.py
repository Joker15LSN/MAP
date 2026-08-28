"""Durable job repository (F-03 / FIX-P0-WORKER-01 / FIX-R2-P0-WORKER).

Claim uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never double
claim. Every state-changing write (heartbeat/complete/fail) is fenced by
``lease_owner + attempt`` AND ``lease_expires_at >= now()`` so a worker
that lost its lease — either to a reclaim or simply to clock time — can
never overwrite state. All fence time comparisons use database time
(``now()``) so worker clock drift can never widen the expiry window.
Reclaim picks one expired job inside a locked transaction instead of
bulk-resetting all expired jobs.

Transactions are owned by the caller (service layer); repository methods
flush/return so the runner can commit in short transactions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job, JobStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OwnershipLost(Exception):
    """The current worker no longer owns the lease (another worker took over)."""


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        job_types: list[str] | None = None,
    ) -> Job | None:
        """Claim one due job (queued, or running with an expired lease).

        ``attempt`` is incremented on every claim; it acts as the fencing
        token for all later writes from this worker. Due/lease comparisons
        use database time so worker clock drift cannot steal or extend a
        lease.
        """
        # Anchor to the database clock once (concrete value) so the lease
        # expiry we store and the fence comparisons below share one time
        # base without worker clock drift.
        db_now = (await self._session.execute(select(func.now()))).scalar_one()
        lease_expires = db_now + timedelta(seconds=lease_seconds)

        stmt = (
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED,
                (Job.next_run_at.is_(None)) | (Job.next_run_at <= db_now),
            )
            .order_by(Job.priority.desc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job_types:
            stmt = stmt.where(Job.job_type.in_(job_types))

        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is not None:
            self._take_lease(job, worker_id, lease_expires)
            await self._session.flush()
            return job

        # Reclaim a single expired-lease running job inside the same locked
        # transaction. No bulk reset: only this job changes state, and the
        # attempt bump makes the old worker's writes fail the fence.
        reclaim = (
            select(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at < db_now,
            )
            .order_by(Job.priority.desc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job_types:
            reclaim = reclaim.where(Job.job_type.in_(job_types))
        job = (await self._session.execute(reclaim)).scalar_one_or_none()
        if job is not None:
            self._take_lease(job, worker_id, lease_expires)
            await self._session.flush()
            return job
        return None

    @staticmethod
    def _take_lease(job: Job, worker_id: str, lease_expires: datetime) -> None:
        job.status = JobStatus.RUNNING
        job.attempt = (job.attempt or 0) + 1
        job.lease_owner = worker_id
        job.lease_expires_at = lease_expires
        job.started_at = job.started_at or _utcnow()  # bookkeeping only

    async def heartbeat(
        self,
        job_id: uuid.UUID,
        *,
        lease_seconds: int = 60,
        owner: str,
        attempt: int,
    ) -> bool:
        """Renew the lease; False when the job is no longer ours.

        Fenced by (id, status=running, lease_owner, attempt) plus a live
        lease (``lease_expires_at >= now()`` in database time) so an
        expired worker can never keep a reclaimed lease alive.
        """
        db_now = func.now()
        result = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING,
                Job.lease_owner == owner,
                Job.attempt == attempt,
                Job.lease_expires_at >= db_now,
            )
            .values(lease_expires_at=db_now + timedelta(seconds=lease_seconds))
        )
        return bool(result.rowcount)

    async def complete(
        self,
        job_id: uuid.UUID,
        result: dict | None = None,
        *,
        owner: str,
        attempt: int,
    ) -> bool:
        """Mark the job succeeded; False means ownership was lost.

        The fence includes the live-lease condition (database time), so a
        worker whose lease expired — even before another worker reclaims —
        can never submit a terminal state.
        """
        now = _utcnow()
        result_update = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING,
                Job.lease_owner == owner,
                Job.attempt == attempt,
                Job.lease_expires_at.isnot(None),
                Job.lease_expires_at >= func.now(),
            )
            .values(
                status=JobStatus.SUCCEEDED,
                result_json=result,
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        return bool(result_update.rowcount)

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        retryable: bool = True,
        owner: str,
        attempt: int,
    ) -> bool:
        """Mark a running job failed; retryable failures return to queued.

        Returns False when the fencing condition failed (ownership lost).
        Implemented with pure UPDATEs so no ORM object mutation can bypass
        the fence on a later flush.
        """
        now = _utcnow()
        result = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING,
                Job.lease_owner == owner,
                Job.attempt == attempt,
                Job.lease_expires_at.isnot(None),
                Job.lease_expires_at >= func.now(),
            )
            .values(
                error_code=error_code,
                error_message=error_message,
                lease_owner=None,
                lease_expires_at=None,
                started_at=None,
            )
        )
        if not result.rowcount:
            return False
        # We hold the fence; the row is locked by our UPDATE. Read the
        # current attempt counters and decide the terminal state in a
        # second statement.
        counters = (
            await self._session.execute(
                select(Job.attempt, Job.max_attempts).where(Job.id == job_id)
            )
        ).one()
        retry_now = retryable and (counters.attempt or 0) < (counters.max_attempts or 3)
        await self._session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.QUEUED if retry_now else JobStatus.FAILED,
                next_run_at=(
                    now + timedelta(seconds=2 ** (counters.attempt or 0)) if retry_now else None
                ),
                finished_at=None if retry_now else now,
            )
        )
        return True

    async def cancel(self, job_id: uuid.UUID) -> Job | None:
        job = await self._session.get(Job, job_id)
        if job is None or job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return None
        job.status = JobStatus.CANCELLED
        job.finished_at = _utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        return job

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)
