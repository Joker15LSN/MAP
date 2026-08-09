"""FIX-P0-WORKER-01 acceptance: lease fencing, split-brain, duplicate effects.

Runs against the real PostgreSQL container:
- 1s lease + 3s handler + two workers: exactly one side effect (E-01)
- heartbeat commits are visible to other sessions
- stale worker's complete/fail is rejected (fence), terminal state intact
- killed worker's lease expires and is reclaimed
- heartbeat DB failure sets lease_lost (fail-closed)
- retry handler replays the same idempotency key without a second effect
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobStatus
from app.repositories.jobs import JobRepository
from app.workers.job_runner import JobRunner, get_current_job_context

pytestmark = pytest.mark.asyncio

WORKSPACE = "00000000-0000-0000-0000-000000000001"


async def _create_job(
    session, *, job_type="test", max_attempts=3, payload=None, idempotency_key=None
):
    job = Job(
        workspace_id=WORKSPACE,
        job_type=job_type,
        payload_json=payload or {"q": 1},
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


def _factory(_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def test_long_handler_with_two_workers_single_side_effect(
    _engine, session, monkeypatch
) -> None:
    """E-01 regression: heartbeat failure => lease lost => no double effect.

    Worker A's heartbeat fails (DB timeout); its handler exits at the next
    safe point without performing the side effect. Worker B reclaims the
    expired lease and performs the effect exactly once.
    """
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)
        job_id = await _first_job_id(factory)

    effects: list[str] = []

    original_heartbeat = JobRepository.heartbeat

    async def flaky_heartbeat(self, job_id, *, lease_seconds=60, owner="", attempt=1):
        if owner == "worker-A":
            raise ConnectionError("simulated db timeout")
        return await original_heartbeat(
            self, job_id, lease_seconds=lease_seconds, owner=owner, attempt=attempt
        )

    monkeypatch.setattr(JobRepository, "heartbeat", flaky_heartbeat)

    async def long_handler(job, session):
        ctx = get_current_job_context()
        assert ctx is not None
        # Long-running work: check the lease at every safe point (0.2s).
        for _ in range(15):
            await asyncio.sleep(0.2)
            if not ctx.lease_ok:
                return None  # lost lease: no side effect, no result
        effects.append(f"effect-by-{ctx.worker_id}")
        return {"ok": True}

    runner_a = JobRunner(
        factory,
        {"test": long_handler},
        worker_id="worker-A",
        lease_seconds=1,
        poll_seconds=0.05,
        heartbeat_interval_seconds=0.3,
    )
    runner_b = JobRunner(
        factory,
        {"test": long_handler},
        worker_id="worker-B",
        lease_seconds=1,
        poll_seconds=0.05,
        heartbeat_interval_seconds=0.3,
    )

    task_a = asyncio.create_task(runner_a.run_once())
    await asyncio.sleep(1.6)  # A's lease expires while A's handler is still running
    await runner_b.run_once()
    await task_a

    assert len(effects) == 1, effects
    async with factory() as s:
        stored = await s.get(Job, job_id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.attempt == 2  # A's claim + B's reclaim
        assert stored.result_json == {"ok": True}


async def _first_job_id(factory) -> None:
    from sqlalchemy import select

    async with factory() as s:
        return (await s.execute(select(Job.id).limit(1))).scalar_one()


async def test_heartbeat_committed_and_visible_to_other_session(_engine, session) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)
        repo = JobRepository(s)
        claimed = await repo.claim_next(worker_id="w1", lease_seconds=60)
        assert claimed is not None
        await s.commit()
        first_expiry = claimed.lease_expires_at
        ok = await repo.heartbeat(claimed.id, lease_seconds=60, owner="w1", attempt=claimed.attempt)
        await s.commit()
        assert ok is True

    # A different session observes the extended lease.
    async with factory() as s2:
        stored = await s2.get(Job, claimed.id)
        assert stored.lease_expires_at > first_expiry


async def test_stale_worker_complete_and_fail_rejected(_engine, session) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)
        repo = JobRepository(s)
        claimed = await repo.claim_next(worker_id="worker-A", lease_seconds=60)
        await s.commit()
        attempt_a = claimed.attempt
        job_id = claimed.id
        # A's lease expires.
        claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await s.commit()

        # B reclaims and succeeds.
        repo_b = JobRepository(s)
        claimed_b = await repo_b.claim_next(worker_id="worker-B", lease_seconds=60)
        assert claimed_b is not None
        assert claimed_b.attempt == attempt_a + 1
        assert await repo_b.complete(
            claimed_b.id, {"by": "B"}, owner="worker-B", attempt=claimed_b.attempt
        )
        await s.commit()

        # Stale A can neither complete nor fail: fence rejects it.
        assert (
            await repo.complete(claimed.id, {"by": "A"}, owner="worker-A", attempt=attempt_a)
            is False
        )
        assert (
            await repo.fail(
                claimed.id,
                error_code="HANDLER_ERROR",
                error_message="late",
                owner="worker-A",
                attempt=attempt_a,
            )
            is False
        )
        await s.rollback()

    async with factory() as s2:
        stored = await s2.get(Job, job_id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.result_json == {"by": "B"}


async def test_killed_worker_lease_expires_and_is_reclaimed(_engine, session) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s)
        job_id = job.id
        repo = JobRepository(s)
        claimed = await repo.claim_next(worker_id="dead-worker", lease_seconds=1)
        await s.commit()
        assert claimed is not None

    # No heartbeat from the dead worker; wait past the lease.
    await asyncio.sleep(1.3)

    ran: list[str] = []
    async with factory() as s:
        repo = JobRepository(s)
        reclaimed = await repo.claim_next(worker_id="fresh-worker", lease_seconds=60)
        assert reclaimed is not None
        assert reclaimed.attempt == 2
        assert await repo.complete(reclaimed.id, {"ok": True}, owner="fresh-worker", attempt=2)
        await s.commit()
        ran.append(str(reclaimed.id))

    async with factory() as s2:
        stored = await s2.get(Job, job_id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.attempt == 2


async def test_heartbeat_db_failure_sets_lease_lost(_engine, session, monkeypatch) -> None:
    """A DB failure during heartbeat must fail closed, not run to expiry."""
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)

    async def always_fail(self, job_id, *, lease_seconds=60, owner="", attempt=1):
        raise ConnectionError("db down")

    monkeypatch.setattr(JobRepository, "heartbeat", always_fail)

    lost = asyncio.Event()

    async def handler(job, session):
        ctx = get_current_job_context()
        # Wait until the heartbeat loop marks the lease lost.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ctx.lease_lost.wait(), timeout=3)
        if ctx.lease_lost.is_set():
            lost.set()
        return None

    runner = JobRunner(
        factory, {"test": handler}, worker_id="w", lease_seconds=1, heartbeat_interval_seconds=0.2
    )
    await runner.run_once()
    assert lost.is_set()


async def test_retry_replays_idempotency_key_with_single_side_effect(_engine, session) -> None:
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, idempotency_key="key-42")

    external: set[str] = set()

    async def handler(job, session):
        ctx = get_current_job_context()
        if ctx.idempotency_key not in external:
            external.add(ctx.idempotency_key)  # the external side effect
            raise RuntimeError("boom on first attempt")
        return {"ok": True}

    runner = JobRunner(factory, {"test": handler}, worker_id="w", poll_seconds=0.05)
    await runner.run_once()  # attempt 1: side effect + failure

    async with factory() as s:
        stored = await s.get(Job, job.id)
        delay = (stored.next_run_at - datetime.now(UTC)).total_seconds()
    await asyncio.sleep(max(0.0, delay + 0.1))

    await runner.run_once()  # attempt 2: replays, no second side effect
    assert external == {"key-42"}

    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.SUCCEEDED
        assert stored.attempt == 2


async def test_sigterm_cancels_inflight_handler_at_safe_point(_engine, session) -> None:
    """SIGTERM (stop_event) must reach the in-flight handler as cancel."""
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)

    stop_event = asyncio.Event()
    cancelled_seen = asyncio.Event()

    async def long_handler(job, session):
        ctx = get_current_job_context()
        while ctx.lease_ok:
            await asyncio.sleep(0.05)
        cancelled_seen.set()
        return None

    runner = JobRunner(factory, {"test": long_handler}, worker_id="w", poll_seconds=0.05)
    task = asyncio.create_task(runner.run_forever(stop_event))
    await asyncio.sleep(0.3)  # handler is running
    stop_event.set()  # SIGTERM
    await asyncio.wait_for(cancelled_seen.wait(), timeout=3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
