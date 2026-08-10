"""R3-P0-01 acceptance: fenced worker transactions + provable effect protocol.

Real-PostgreSQL proofs for every crash window of the effect protocol
(``pending -> dispatching -> delivered | uncertain``), each window run 20
consecutive rounds with the external fake provider's action count asserted
to be EXACTLY 1 (never 0, never 2):

- W1 crash BEFORE the intent commits
- W2 crash AFTER the intent, BEFORE the external call
- W3 crash AFTER the call started, BEFORE the confirmation is persisted
- W4 crash AFTER the confirmation, BEFORE the job completes

plus:

- provider unknown/timeout -> effect ``uncertain`` + job ``EFFECT_UNCERTAIN``
  (never faked ``succeeded``);
- empty/None effect keys are rejected (no shared cross-job key);
- message_reconcile handler runs on the runner session: a stale worker
  whose lease expired (or whose heartbeat failed) persists ZERO message
  writes, and the job is reclaimed by a fresh owner whose fenced
  completion commits business writes + ``Job.status=succeeded`` atomically.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import EFFECT_UNCERTAIN, EffectLedger, Job, JobStatus, Message
from app.repositories.jobs import JobRepository
from app.workers.job_runner import (
    EffectGuard,
    JobRunner,
    get_current_job_context,
)

WORKSPACE = "00000000-0000-0000-0000-000000000001"
ROUNDS = 20


class SimulatedCrash(Exception):
    """Process death: nothing after this point commits."""


def _factory(_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _create_job(factory, *, key: str, max_attempts: int = 5) -> uuid.UUID:
    async with factory() as s:
        job = Job(
            workspace_id=WORKSPACE,
            job_type="effect_test",
            payload_json={"q": 1},
            max_attempts=max_attempts,
            idempotency_key=key,
        )
        s.add(job)
        await s.commit()
        await s.refresh(job)
        return job.id


async def _reset_backoff(factory, job_id: uuid.UUID) -> None:
    """A simulated crash leaves the job running/requeued; make the retry due
    immediately instead of sleeping out the exponential backoff."""
    async with factory() as s:
        await s.execute(
            text("UPDATE map_control.jobs SET next_run_at = NULL WHERE id = :id"),
            {"id": job_id},
        )
        await s.commit()


async def _job_row(factory, job_id: uuid.UUID) -> Job:
    async with factory() as s:
        return await s.get(Job, job_id)


async def _effect_state(factory, key: str) -> str | None:
    async with factory() as s:
        return (
            await s.execute(
                select(EffectLedger.status).where(
                    EffectLedger.workspace_id == uuid.UUID(WORKSPACE),
                    EffectLedger.effect_key == key,
                )
            )
        ).scalar_one_or_none()


def _guarded_handler(effects: list[str], crash_point: str | None):
    """One handler factory for all four windows; ``crash_point`` selects the
    simulated process death for the FIRST attempt of each round."""

    async def handler(job, session):
        ctx = get_current_job_context()
        guard = EffectGuard(ctx.session_factory)

        if crash_point == "before_intent":
            raise SimulatedCrash("killed before the intent could commit")

        if crash_point == "after_intent":
            await guard.record_intent(ctx.idempotency_key, job.workspace_id, job_id=job.id)
            raise SimulatedCrash("killed after intent commit, before the call")

        async def provider():
            effects.append("external-effect")
            return True

        await guard.run_effect_once(
            ctx.idempotency_key, job.workspace_id, provider, job_id=job.id
        )
        if crash_point == "after_ack":
            raise SimulatedCrash("killed after ack commit, before job complete")
        return {"ok": True}

    return handler


async def _run_window(factory, window: str, crash_point: str | None) -> tuple[list, uuid.UUID, str]:
    """One round of window ``window``: attempt 1 crashes at ``crash_point``,
    attempt 2 runs to its natural outcome. Returns (effects, job_id, key)."""
    key = f"{window}-{uuid.uuid4().hex[:12]}"
    job_id = await _create_job(factory, key=key)
    effects: list[str] = []

    runner = JobRunner(
        factory, {"effect_test": _guarded_handler(effects, crash_point)},
        worker_id=f"{window}-p1", poll_seconds=0.05,
    )
    await runner.run_once()  # attempt 1 (crashes unless crash_point is None)
    await _reset_backoff(factory, job_id)

    # "Restart": fresh runner instance sharing only the database.
    runner2 = JobRunner(
        factory, {"effect_test": _guarded_handler(effects, None)},
        worker_id=f"{window}-p2", poll_seconds=0.05,
    )
    await runner2.run_once()  # attempt 2
    return effects, job_id, key


# ---------------------------------------------------------------------------
# W1: crash before intent — retry performs the effect exactly once.
# ---------------------------------------------------------------------------


async def test_window1_crash_before_intent_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    for rnd in range(ROUNDS):
        effects, job_id, key = await _run_window(factory, "w1", "before_intent")
        assert effects == ["external-effect"], f"round {rnd}: {effects}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered"


# ---------------------------------------------------------------------------
# W2: intent committed, call never started — retry proceeds (never skips).
# ---------------------------------------------------------------------------


async def test_window2_after_intent_before_effect_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    for rnd in range(ROUNDS):
        effects, job_id, key = await _run_window(factory, "w2", "after_intent")
        assert effects == ["external-effect"], f"round {rnd}: {effects}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered"


# ---------------------------------------------------------------------------
# W3: call started, confirmation never persisted — terminal uncertain,
#     NEVER replayed, NEVER faked succeeded.
# ---------------------------------------------------------------------------


async def test_window3_after_effect_before_ack_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    for rnd in range(ROUNDS):
        key = f"w3-{uuid.uuid4().hex[:12]}"
        job_id = await _create_job(factory, key=key)
        effects: list[str] = []

        async def crashing_ack(self, k, ws):
            raise SimulatedCrash("kill -9 between provider ack and ledger commit")

        original_ack = EffectGuard.ack_effect
        EffectGuard.ack_effect = crashing_ack  # type: ignore[method-assign]
        try:
            runner = JobRunner(
                factory, {"effect_test": _guarded_handler(effects, None)},
                worker_id="w3-p1", poll_seconds=0.05,
            )
            await runner.run_once()  # attempt 1: provider called, ack lost
        finally:
            EffectGuard.ack_effect = original_ack  # type: ignore[method-assign]
        await _reset_backoff(factory, job_id)

        runner2 = JobRunner(
            factory, {"effect_test": _guarded_handler(effects, None)},
            worker_id="w3-p2", poll_seconds=0.05,
        )
        await runner2.run_once()  # attempt 2: dispatching -> uncertain

        # The provider was called EXACTLY once: not skipped (0), replayed (2).
        assert effects == ["external-effect"], f"round {rnd}: {effects}"
        job = await _job_row(factory, job_id)
        # The outcome is unknown: never fake succeeded.
        assert job.status == JobStatus.FAILED, f"round {rnd}: {job.status}"
        assert job.error_code == "EFFECT_UNCERTAIN", f"round {rnd}: {job.error_code}"
        assert await _effect_state(factory, key) == EFFECT_UNCERTAIN


# ---------------------------------------------------------------------------
# W4: confirmation committed, job completion lost — retry skips, succeeds.
# ---------------------------------------------------------------------------


async def test_window4_after_ack_before_job_complete_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    for rnd in range(ROUNDS):
        effects, job_id, key = await _run_window(factory, "w4", "after_ack")
        assert effects == ["external-effect"], f"round {rnd}: {effects}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered"


# ---------------------------------------------------------------------------
# Provider unknown/timeout: never fake success.
# ---------------------------------------------------------------------------


async def test_provider_unknown_never_fakes_succeeded(_engine, session) -> None:
    factory = _factory(_engine)

    for outcome in ("returned_false", "raised"):
        key = f"unknown-{uuid.uuid4().hex[:12]}"
        job_id = await _create_job(factory, key=key)

        async def provider(_outcome=outcome):
            if _outcome == "raised":
                raise ConnectionError("provider timeout")
            return False  # unknown / unconfirmed

        async def handler(job, session):
            from app.workers.job_runner import get_current_job_context

            ctx = get_current_job_context()
            guard = EffectGuard(ctx.session_factory)
            await guard.run_effect_once(
                ctx.idempotency_key, job.workspace_id, provider, job_id=job.id
            )
            return {"ok": True}

        runner = JobRunner(factory, {"effect_test": handler}, worker_id="w", poll_seconds=0.05)
        await runner.run_once()

        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.FAILED, outcome
        assert job.error_code == "EFFECT_UNCERTAIN", outcome
        assert await _effect_state(factory, key) == EFFECT_UNCERTAIN

        # A retry must not replay an uncertain effect either.
        await _reset_backoff(factory, job_id)
        async with factory() as s:
            await s.execute(
                text(
                    "UPDATE map_control.jobs SET status = 'queued', error_code = NULL "
                    "WHERE id = :id"
                ),
                {"id": job_id},
            )
            await s.commit()
        await runner.run_once()
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.FAILED
        assert job.error_code == "EFFECT_UNCERTAIN"
        assert await _effect_state(factory, key) == EFFECT_UNCERTAIN


async def test_effect_key_must_be_non_empty(_engine, session) -> None:
    factory = _factory(_engine)
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)
    for bad in (None, "", "   "):
        with pytest.raises(ValueError):
            await guard.record_intent(bad, ws)
        with pytest.raises(ValueError):
            await guard.run_effect_once(bad, ws, provider=_never_called)


async def _never_called() -> bool:  # pragma: no cover - must never run
    raise AssertionError("provider must not run without a valid key")


# ---------------------------------------------------------------------------
# message_reconcile: fenced transaction discipline (problem A).
# ---------------------------------------------------------------------------


async def _seed_stale_message(factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a conversation with a stale streaming assistant message."""
    from app.repositories.conversations import ConversationRepository

    async with factory() as s:
        repo = ConversationRepository(s)
        conversation = await repo.create_conversation(
            workspace_id=uuid.UUID(WORKSPACE),
            owner_user_id="local-admin",
            mode="global",
            title="reconcile fencing",
        )
        await s.commit()
        _, assistant = await repo.create_message_pair(
            conversation=conversation, request_id=f"req-{uuid.uuid4().hex[:8]}", user_content="hi"
        )
        await s.execute(
            text("UPDATE map_control.messages SET updated_at = :ts WHERE id = :id"),
            {"ts": datetime.now(UTC) - timedelta(hours=1), "id": assistant.id},
        )
        await s.commit()
        return conversation.id, assistant.id


async def _enqueue_reconcile_job(factory) -> uuid.UUID:
    async with factory() as s:
        job = Job(
            workspace_id=WORKSPACE,
            job_type="message_reconcile",
            payload_json={},
            max_attempts=3,
        )
        s.add(job)
        await s.commit()
        await s.refresh(job)
        return job.id


async def _message_status(factory, message_id: uuid.UUID) -> str:
    async with factory() as s:
        return (await s.get(Message, message_id)).status


async def test_reconcile_expired_lease_writes_zero_messages(
    _engine, session, monkeypatch
) -> None:
    """Lease expiry AFTER the handler's UPDATE: the fenced complete rejects
    and the runner rolls the message writes back — the stale worker's
    persisted write count is 0; a fresh owner reclaims and commits."""
    from app.workers.main import _message_reconcile_handler

    factory = _factory(_engine)
    monkeypatch.setenv("MAP_RECONCILE_STALE_AFTER_S", "60")
    _, message_id = await _seed_stale_message(factory)
    job_id = await _enqueue_reconcile_job(factory)

    # Heartbeat lies so only the DATABASE fence can stop the stale commit.
    async def lying_heartbeat(self, job_id, *, lease_seconds=60, owner="", attempt=1):
        return True

    monkeypatch.setattr(JobRepository, "heartbeat", lying_heartbeat)

    async def slow_handler(job, s):
        await asyncio.sleep(1.3)  # outlive the 1s lease, THEN do the work
        return await _message_reconcile_handler(job, s)

    stale = JobRunner(
        factory, {"message_reconcile": slow_handler},
        worker_id="stale-worker", lease_seconds=1,
        poll_seconds=0.05, heartbeat_interval_seconds=0.2,
    )
    await stale.run_once()

    # Stale worker: complete rejected -> rollback -> message untouched.
    assert await _message_status(factory, message_id) == "streaming"
    job = await _job_row(factory, job_id)
    assert job.status == JobStatus.RUNNING

    # Fresh owner reclaims the expired lease and commits atomically.
    fresh = JobRunner(
        factory, {"message_reconcile": _message_reconcile_handler},
        worker_id="fresh-worker", lease_seconds=60, poll_seconds=0.05,
    )
    await fresh.run_once()

    assert await _message_status(factory, message_id) == "failed"
    job = await _job_row(factory, job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.attempt == 2
    assert job.result_json == {"reconciled": 1}


async def test_reconcile_heartbeat_failure_writes_zero_messages(
    _engine, session, monkeypatch
) -> None:
    """Heartbeat failure BEFORE the UPDATE: the handler observes lease-lost
    at its safe point, produces zero writes, and the job is reclaimed."""
    from app.workers.main import _message_reconcile_handler

    factory = _factory(_engine)
    monkeypatch.setenv("MAP_RECONCILE_STALE_AFTER_S", "60")
    _, message_id = await _seed_stale_message(factory)
    job_id = await _enqueue_reconcile_job(factory)

    async def failing_heartbeat(self, job_id, *, lease_seconds=60, owner="", attempt=1):
        raise ConnectionError("simulated db timeout")

    original_heartbeat = JobRepository.heartbeat
    monkeypatch.setattr(JobRepository, "heartbeat", failing_heartbeat)

    async def waits_for_loss(job, s):
        ctx = get_current_job_context()
        deadline = asyncio.get_running_loop().time() + 3.0
        while ctx.lease_ok and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        # A handler that observes the lost lease must surface it (raise),
        # not return a success result — the runner then attempts the fenced
        # fail write, which the expired-lease fence rejects.
        result = await _message_reconcile_handler(job, s)
        if not ctx.lease_ok:
            raise RuntimeError("lease lost during message reconcile")
        return result

    stale = JobRunner(
        factory, {"message_reconcile": waits_for_loss},
        worker_id="stale-worker", lease_seconds=1,
        poll_seconds=0.05, heartbeat_interval_seconds=0.2,
    )
    await stale.run_once()
    monkeypatch.setattr(JobRepository, "heartbeat", original_heartbeat)

    # Zero writes from the stale worker; the message stays streaming.
    assert await _message_status(factory, message_id) == "streaming"

    # The failed heartbeat returned the job to the queue (fail-closed,
    # retryable). Skip the retry backoff, then a fresh owner claims it.
    await _reset_backoff(factory, job_id)
    fresh = JobRunner(
        factory, {"message_reconcile": _message_reconcile_handler},
        worker_id="fresh-worker", lease_seconds=60, poll_seconds=0.05,
    )
    await fresh.run_once()

    assert await _message_status(factory, message_id) == "failed"
    job = await _job_row(factory, job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.result_json == {"reconciled": 1}
