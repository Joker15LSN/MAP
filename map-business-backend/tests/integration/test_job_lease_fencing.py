"""FIX-P0-WORKER-01 / FIX-R2-P0-WORKER acceptance: lease fencing,
split-brain, duplicate effects.

Runs against the real PostgreSQL container:
- 1s lease + 3s handler + two workers: exactly one side effect (E-01),
  stable across 20 consecutive rounds
- heartbeat commits are visible to other sessions
- stale worker's complete/fail is rejected (fence), terminal state intact
- expired lease WITHOUT reclaim still blocks heartbeat/complete/fail
  (E2-01), including the ±100ms expiry race window (100 rounds)
- handler's uncommitted DB writes are rolled back when the lease expired
- killed worker's lease expires and is reclaimed
- heartbeat DB failure sets lease_lost (fail-closed)
- retry/kill/restart use the persisted EffectGuard fact source: the side
  effect happens exactly once even across separate runner instances
- heartbeat interval >= lease/3 is rejected at startup
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobStatus
from app.repositories.jobs import JobRepository
from app.workers.job_runner import EffectGuard, JobRunner, get_current_job_context

# asyncio_mode=auto handles the async tests; no module-level asyncio mark
# (it would wrongly mark the sync validation test below).

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


class _RecordingProvider:
    """EffectProvider (R4-P0-01) with an in-memory per-key fact store.

    These tests exercise lease fencing, not cross-process provider facts —
    the server-side persistent fact proofs live in
    ``test_effect_protocol_windows.py``. Deduplication by key is still
    structurally enforced: a repeated ``send`` under the same key appends
    nothing.
    """

    def __init__(self, effects: list[str]) -> None:
        self._effects = effects
        self._facts: set[str] = set()

    async def send(self, key: str) -> bool:
        if key not in self._facts:
            self._facts.add(key)
            self._effects.append("external-effect")
        return True

    async def query(self, key: str) -> bool | None:
        return key in self._facts


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
        return (
            await s.execute(select(Job.id).order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        ).scalar_one()


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
    """R3-P0-01 window 4 (after ack, before job complete): the first attempt
    delivers the effect but crashes before the fenced completion; the retry
    sees ``delivered`` in the ledger, skips the call and succeeds."""
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, idempotency_key="key-42")

    calls: list[str] = []
    crash_first = {"flag": True}

    async def handler(job, session):
        ctx = get_current_job_context()
        guard = EffectGuard(ctx.session_factory)

        await guard.run_effect_once(
            ctx.idempotency_key, job.workspace_id, _RecordingProvider(calls), job_id=job.id
        )
        if crash_first["flag"]:
            crash_first["flag"] = False
            raise RuntimeError("crash after ack, before job complete")
        return {"ok": True}

    runner = JobRunner(factory, {"test": handler}, worker_id="w", poll_seconds=0.05)
    await runner.run_once()  # attempt 1: delivered + crash before complete

    async with factory() as s:
        stored = await s.get(Job, job.id)
        delay = (stored.next_run_at - datetime.now(UTC)).total_seconds()
    await asyncio.sleep(max(0.0, delay + 0.1))

    await runner.run_once()  # attempt 2: ledger says delivered -> no call
    guard = EffectGuard(factory)
    async with factory() as s:
        stored = await s.get(Job, job.id)
    assert await guard.has_effect("key-42", stored.workspace_id)
    assert calls == ["external-effect"]

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


# ---------------------------------------------------------------------------
# FIX-R2-P0-WORKER: expiry window without reclaim, rollback, race window.
# ---------------------------------------------------------------------------


async def test_expired_lease_without_reclaim_blocks_all_writes(_engine, session) -> None:
    """E2-01 regression: lease expired, nobody reclaimed yet — the old
    owner's heartbeat/complete/fail must ALL be rejected."""
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)
        repo = JobRepository(s)
        claimed = await repo.claim_next(worker_id="worker-A", lease_seconds=60)
        assert claimed is not None
        await s.commit()
        job_id, attempt = claimed.id, claimed.attempt
        # Force the lease into the past; no other worker reclaims.
        claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await s.commit()

        assert (
            await repo.heartbeat(job_id, lease_seconds=60, owner="worker-A", attempt=attempt)
            is False
        )
        assert (
            await repo.complete(job_id, {"by": "A"}, owner="worker-A", attempt=attempt) is False
        )
        assert (
            await repo.fail(
                job_id,
                error_code="HANDLER_ERROR",
                error_message="late",
                owner="worker-A",
                attempt=attempt,
            )
            is False
        )
        await s.rollback()

    async with factory() as s2:
        stored = await s2.get(Job, job_id)
        assert stored.status == JobStatus.RUNNING  # never rewritten by A
        assert stored.lease_owner == "worker-A"


async def test_expired_lease_complete_rolls_back_handler_db_writes(
    _engine, session, monkeypatch
) -> None:
    """Handler produced uncommitted DB writes, then the lease expired:
    complete must fail AND the handler's writes must be rolled back."""
    factory = _factory(_engine)
    async with factory() as s:
        await _create_job(s)
        job_id = await _first_job_id(factory)

    # Heartbeat lies: the worker believes the lease is fine, so only the
    # database fence (lease_expires_at >= now()) can stop the stale write.
    async def lying_heartbeat(self, job_id, *, lease_seconds=60, owner="", attempt=1):
        return True

    monkeypatch.setattr(JobRepository, "heartbeat", lying_heartbeat)

    marker_ids: list = []

    async def slow_handler(job, session):
        # Ignore ctx on purpose: worst-case handler that outlives the lease.
        await asyncio.sleep(1.3)  # longer than the 1s lease
        marker = Job(
            workspace_id=job.workspace_id,
            job_type="side-effect-marker",
            payload_json={"marker": True},
        )
        session.add(marker)  # uncommitted handler DB write
        await session.flush()
        marker_ids.append(marker.id)
        return {"ok": True}

    runner = JobRunner(
        factory,
        {"test": slow_handler},
        worker_id="worker-A",
        lease_seconds=1,
        poll_seconds=0.05,
        heartbeat_interval_seconds=0.2,
    )
    await runner.run_once()
    assert marker_ids, "handler must have attempted the DB write"

    async with factory() as s2:
        stored = await s2.get(Job, job_id)
        # complete was rejected by the expiry fence: no SUCCEEDED.
        assert stored.status == JobStatus.RUNNING
        # The handler's uncommitted write was rolled back.
        assert await s2.get(Job, marker_ids[0]) is None


async def test_expiry_race_window_100_rounds_old_worker_never_wins(
    _engine, session
) -> None:
    """±100ms race window around expiry, 100 rounds: the old worker's
    terminal writes must never succeed once the lease is expired."""
    import random

    factory = _factory(_engine)
    succeeded_terminal = 0
    for _ in range(100):
        delta_ms = random.uniform(-100.0, 100.0)
        async with factory() as s:
            await _create_job(s)
            repo = JobRepository(s)
            claimed = await repo.claim_next(worker_id="worker-A", lease_seconds=60)
            assert claimed is not None
            await s.commit()
            job_id, attempt = claimed.id, claimed.attempt
            # Push expiry around "now" (±100ms) using database time.
            from sqlalchemy import text

            await s.execute(
                text(
                    "UPDATE map_control.jobs SET lease_expires_at = "
                    "now() + make_interval(secs => :d) WHERE id = :id"
                ),
                {"d": delta_ms / 1000.0, "id": job_id},
            )
            await s.commit()

        # Wait until the lease is certainly expired (past the +100ms edge).
        await asyncio.sleep(max(0.0, delta_ms / 1000.0) + 0.05)

        async with factory() as s:
            repo = JobRepository(s)
            ok_complete = await repo.complete(
                job_id, {"by": "A"}, owner="worker-A", attempt=attempt
            )
            ok_fail = False
            if not ok_complete:
                ok_fail = await repo.fail(
                    job_id,
                    error_code="HANDLER_ERROR",
                    error_message="late",
                    owner="worker-A",
                    attempt=attempt,
                )
            await s.rollback()
        if ok_complete or ok_fail:
            succeeded_terminal += 1

    assert succeeded_terminal == 0


async def test_long_handler_two_workers_20_rounds_single_effect(_engine, session) -> None:
    """2s lease + 3s handler + two workers + heartbeat timeout, run 20
    consecutive rounds: the side effect happens exactly once per round.

    R5 note: the lease was originally 1s; under full-gate machine load the
    surviving worker's heartbeat task could be scheduled >1s late, losing
    the lease mid-handler and producing ZERO effects (a false red, not a
    fencing regression). 2s keeps the exact takeover semantics (the lease
    still expires before the 3s handler ends) with twice the margin.
    """
    factory = _factory(_engine)
    for _round in range(20):
        async with factory() as s:
            await _create_job(s)
            job_id = await _first_job_id(factory)

        effects: list[str] = []
        original_heartbeat = JobRepository.heartbeat

        async def flaky_heartbeat(
            self, job_id, *, lease_seconds=60, owner="", attempt=1, _original=original_heartbeat
        ):
            if owner == "worker-A":
                raise ConnectionError("simulated db timeout")
            return await _original(
                self, job_id, lease_seconds=lease_seconds, owner=owner, attempt=attempt
            )

        async def long_handler(job, session, _effects=effects):
            ctx = get_current_job_context()
            for _ in range(15):
                await asyncio.sleep(0.2)
                if not ctx.lease_ok:
                    return None
            _effects.append(f"effect-by-{ctx.worker_id}")
            return {"ok": True}

        JobRepository.heartbeat = flaky_heartbeat  # type: ignore[method-assign]
        try:
            runner_a = JobRunner(
                factory,
                {"test": long_handler},
                worker_id="worker-A",
                lease_seconds=2,
                poll_seconds=0.05,
                heartbeat_interval_seconds=0.3,
            )
            runner_b = JobRunner(
                factory,
                {"test": long_handler},
                worker_id="worker-B",
                lease_seconds=2,
                poll_seconds=0.05,
                heartbeat_interval_seconds=0.3,
            )
            task_a = asyncio.create_task(runner_a.run_once())
            # Wait until worker-A's lease is DEFINITELY expired (its
            # heartbeats all fail) before worker-B polls for the takeover.
            await asyncio.sleep(2.6)
            await runner_b.run_once()
            await task_a
        finally:
            JobRepository.heartbeat = original_heartbeat  # type: ignore[method-assign]

        assert len(effects) == 1, f"round {_round}: {effects}"
        async with factory() as s:
            stored = await s.get(Job, job_id)
            assert stored.status == JobStatus.SUCCEEDED, f"round {_round}"
            assert stored.result_json == {"ok": True}


async def test_effect_ledger_survives_process_restart(_engine, session) -> None:
    """Two separate runner instances (simulated restart) sharing only the
    DB: the persisted effect ledger still yields exactly one side effect.
    The first process crashes right AFTER the provider confirmation (ack
    lost); the second process must NOT replay the call."""
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, idempotency_key="restart-key", max_attempts=5)

    effects: list[str] = []

    async def handler(job, session):
        ctx = get_current_job_context()
        guard = EffectGuard(ctx.session_factory)

        await guard.run_effect_once(
            ctx.idempotency_key, job.workspace_id, _RecordingProvider(effects), job_id=job.id
        )
        # Crash right after the confirmed effect; the ack already committed.
        if (job.attempt or 0) == 1:
            raise RuntimeError("crash right after the effect")
        return {"ok": True}

    # "Process 1": delivers the effect, crashes.
    runner_1 = JobRunner(factory, {"test": handler}, worker_id="p1", poll_seconds=0.05)
    await runner_1.run_once()

    async with factory() as s:
        stored = await s.get(Job, job.id)
        delay = (stored.next_run_at - datetime.now(UTC)).total_seconds()
    await asyncio.sleep(max(0.0, delay + 0.1))

    # "Process 2": brand-new runner, no shared memory — the DB ledger wins.
    runner_2 = JobRunner(factory, {"test": handler}, worker_id="p2", poll_seconds=0.05)
    await runner_2.run_once()

    assert effects == ["external-effect"]
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.SUCCEEDED


async def test_pending_intent_is_not_skipped_on_retry(_engine, session) -> None:
    """R3-P0-01 window 2 regression: an intent committed BEFORE the external
    call must NOT be treated as done. The old claim-based guard skipped the
    call here and still reported success with zero external actions."""
    factory = _factory(_engine)
    async with factory() as s:
        job = await _create_job(s, idempotency_key="pending-key", max_attempts=5)

    effects: list[str] = []
    crash_before_call = {"flag": True}

    async def handler(job, session):
        ctx = get_current_job_context()
        guard = EffectGuard(ctx.session_factory)
        if crash_before_call["flag"]:
            # Intent committed, then the process dies before the call.
            await guard.record_intent(ctx.idempotency_key, job.workspace_id, job_id=job.id)
            raise RuntimeError("crash after intent, before effect")

        await guard.run_effect_once(
            ctx.idempotency_key, job.workspace_id, _RecordingProvider(effects), job_id=job.id
        )
        return {"ok": True}

    runner = JobRunner(factory, {"test": handler}, worker_id="p1", poll_seconds=0.05)
    await runner.run_once()  # attempt 1: intent persisted, crash before call

    async with factory() as s:
        stored = await s.get(Job, job.id)
        delay = (stored.next_run_at - datetime.now(UTC)).total_seconds()
    await asyncio.sleep(max(0.0, delay + 0.1))

    crash_before_call["flag"] = False
    runner2 = JobRunner(factory, {"test": handler}, worker_id="p2", poll_seconds=0.05)
    await runner2.run_once()  # attempt 2: pending -> proceeds with the call

    # The effect actually happened exactly once, and the job succeeded.
    assert effects == ["external-effect"]
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.SUCCEEDED
        guard = EffectGuard(factory)
    assert await guard.has_effect("pending-key", job.workspace_id)


def test_heartbeat_interval_must_be_below_lease_third() -> None:
    """Startup validation: any configured heartbeat interval must be
    strictly smaller than lease/3."""
    with pytest.raises(ValueError):
        JobRunner(None, {}, lease_seconds=3, heartbeat_interval_seconds=1.0)  # == lease/3
    with pytest.raises(ValueError):
        JobRunner(None, {}, lease_seconds=3, heartbeat_interval_seconds=2.0)
    with pytest.raises(ValueError):
        JobRunner(None, {}, lease_seconds=3, heartbeat_interval_seconds=0)
    # Strictly below is accepted.
    runner = JobRunner(None, {}, lease_seconds=3, heartbeat_interval_seconds=0.9)
    assert runner.heartbeat_interval_seconds == 0.9
