"""F-03 acceptance: job claim/lease/retry with a real PostgreSQL.

Covers: two workers never double-claim, expired leases are reclaimed,
retryable failures return to queued, cancellation works.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobStatus
from app.repositories.jobs import JobRepository
from app.workers.job_runner import JobRunner

pytestmark = pytest.mark.asyncio


async def _create_job(session, *, job_type="test", max_attempts=3, payload=None):
    job = Job(
        workspace_id="00000000-0000-0000-0000-000000000001",
        job_type=job_type,
        payload_json=payload or {"q": 1},
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


def _factory(_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def test_two_workers_claim_a_job_exactly_once(_engine) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s)
    ran: list[str] = []

    async def handler(job, session):
        await asyncio.sleep(0.05)
        ran.append(job.id.hex)
        return {"ok": True}

    runner_a = JobRunner(factory, {"test": handler}, worker_id="worker-a", poll_seconds=0.01)
    runner_b = JobRunner(factory, {"test": handler}, worker_id="worker-b", poll_seconds=0.01)

    # Both race to claim the single job; exactly one must execute it.
    await asyncio.gather(runner_a.run_once(), runner_b.run_once())

    assert len(ran) == 1
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.result_json == {"ok": True}
        assert stored.attempt == 1


async def test_expired_lease_is_reclaimed(_engine) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s)
        # Simulate a worker that died after claiming: lease in the past.
        repo = JobRepository(s)
        claimed = await repo.claim_next(worker_id="dead-worker", lease_seconds=60)
        assert claimed is not None
        claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await s.commit()

    ran: list[str] = []

    async def handler(job, session):
        ran.append(job.id.hex)
        return {"ok": True}

    runner = JobRunner(factory, {"test": handler}, worker_id="fresh-worker", poll_seconds=0.01)
    assert await runner.run_once() is True
    assert job.id.hex in ran

    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.attempt == 2  # original claim + reclaim


async def test_retryable_failure_returns_to_queued_then_succeeds(_engine) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, max_attempts=3)

    attempts = {"n": 0}

    async def flaky_handler(job, session):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    runner = JobRunner(factory, {"test": flaky_handler}, worker_id="w", poll_seconds=0.01)
    assert await runner.run_once() is True
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.QUEUED
        assert stored.error_code == "HANDLER_ERROR"
        assert stored.attempt == 1
        # Backoff: wait until the job's next_run_at becomes due.
        from datetime import datetime

        delay = (stored.next_run_at - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(max(0.0, delay + 0.1))

    # Second run picks it up again and succeeds.
    assert await runner.run_once() is True
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.attempt == 2


async def test_exhausted_attempts_mark_failed(_engine) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, max_attempts=1)

    async def always_fail(job, session):
        raise RuntimeError("always")

    runner = JobRunner(factory, {"test": always_fail}, worker_id="w", poll_seconds=0.01)
    await runner.run_once()
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.FAILED
        assert stored.error_code == "HANDLER_ERROR"
        assert stored.attempt == 1


async def test_cancel_queued_job(_engine) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s)
        repo = JobRepository(s)
        cancelled = await repo.cancel(job.id)
        assert cancelled is not None
        assert cancelled.status == JobStatus.CANCELLED
        await s.commit()

    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.CANCELLED


async def test_unknown_job_type_fails_without_retry(_engine) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, job_type="nope")

    runner = JobRunner(factory, {}, worker_id="w", poll_seconds=0.01)
    assert await runner.run_once() is True
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.FAILED
        assert stored.error_code == "UNKNOWN_JOB_TYPE"
