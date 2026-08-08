"""Durable job repository (F-03).

Claim uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never double
claim; expired leases are reclaimable. Transactions are owned by the
caller (service layer).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job, JobStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

        ``attempt`` is incremented on every claim so retries are visible.
        """
        now = _utcnow()
        lease_expires = now + timedelta(seconds=lease_seconds)
        stmt = (
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED,
                (Job.next_run_at.is_(None)) | (Job.next_run_at <= now),
            )
            .order_by(Job.priority.desc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job_types:
            stmt = stmt.where(Job.job_type.in_(job_types))

        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is not None:
            job.status = JobStatus.RUNNING
            job.attempt = (job.attempt or 0) + 1
            job.lease_owner = worker_id
            job.lease_expires_at = lease_expires
            job.started_at = job.started_at or now
            await self._session.flush()
            return job

        # Reclaim jobs whose lease expired while running.
        reclaim = (
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at < now,
            )
            .values(
                status=JobStatus.QUEUED,
                lease_owner=None,
                lease_expires_at=None,
                started_at=None,
            )
            .returning(Job.id)
        )
        reclaimed = (await self._session.execute(reclaim)).scalars().all()
        if reclaimed:
            await self._session.commit()
            return await self.claim_next(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                job_types=job_types,
            )
        return None

    async def heartbeat(self, job_id: uuid.UUID, *, lease_seconds: int = 60) -> bool:
        """Renew the lease; returns False if the job is no longer ours."""
        now = _utcnow()
        result = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING,
                Job.lease_expires_at >= now,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        return bool(result.rowcount)

    async def complete(self, job_id: uuid.UUID, result: dict | None = None) -> Job | None:
        job = await self._get_running(job_id)
        if job is None:
            return None
        job.status = JobStatus.SUCCEEDED
        job.result_json = result
        job.finished_at = _utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        return job

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        retryable: bool = True,
    ) -> Job | None:
        """Mark a running job failed; retryable failures return to queued."""
        job = await self._get_running(job_id)
        if job is None:
            return None
        job.error_code = error_code
        job.error_message = error_message
        if retryable and (job.attempt or 0) < (job.max_attempts or 3):
            job.status = JobStatus.QUEUED
            job.lease_owner = None
            job.lease_expires_at = None
            job.next_run_at = _utcnow() + timedelta(seconds=2 ** (job.attempt or 0))
            job.started_at = None
        else:
            job.status = JobStatus.FAILED
            job.finished_at = _utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
        return job

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

    async def _get_running(self, job_id: uuid.UUID) -> Job | None:
        result = await self._session.execute(
            select(Job).where(Job.id == job_id, Job.status == JobStatus.RUNNING)
        )
        return result.scalar_one_or_none()
