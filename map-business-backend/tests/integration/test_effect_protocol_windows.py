"""R3-P0-01 + R4-P0-01 acceptance: fenced worker transactions + provable
exactly-once effect protocol.

Real-PostgreSQL proofs for EVERY crash window of the effect protocol
(``pending -> dispatching -> delivered | uncertain``), each window run 20
consecutive rounds with the provider-side SERVER fact count asserted to be
EXACTLY 1 (never 0, never 2):

- W1  crash BEFORE the intent commits
- W2  crash AFTER the intent, BEFORE the dispatch transition
- W2b crash AFTER ``begin_dispatch()`` commits, BEFORE the provider
      callable's first line (the R4-P0-01 window: recovery must query the
      provider by idempotency key and re-send, not lose the action)
- W3  crash AFTER the provider action, BEFORE the ack is persisted
- W4  crash AFTER the ack, BEFORE the job completes

plus:

- provider unknown/timeout that the provider CANNOT confirm by key ->
  effect ``uncertain`` + job ``EFFECT_UNCERTAIN`` (never faked
  ``succeeded``); a provider that CAN confirm recovers to ``delivered``
  (covered by W3 above);
- empty/None effect keys are rejected (no shared cross-job key);
- two CONCURRENT runners with the SAME key produce exactly one external
  action and the live dispatch is never prematurely marked uncertain;
- R5-P0-01 dispatch generation fence: a LIVE lease is never adopted, an
  expired one has exactly ONE compare-and-set winner, a superseded owner's
  ack/mark_uncertain always hit ``rowcount = 0`` (before AND after the
  winner settles), ``query -> None`` never terminalizes someone else's live
  dispatch, provider-fact reconciliation is monotonic, pre-migration rows
  (NULL token) stay adoptable exactly once, and the protocol-level barrier
  (owner inside the provider call while its lease expires and a second
  worker legitimately takes over) still ends in ONE action + ``delivered``;
- message_reconcile handler runs on the runner session: a stale worker
  whose lease expired (or whose heartbeat failed) persists ZERO message
  writes, and the job is reclaimed by a fresh owner whose fenced
  completion commits business writes + ``Job.status=succeeded`` atomically.

R4-P0-01: the external provider implements :class:`EffectProvider` — the
idempotency key is FORCED into ``send()`` and ``query()`` resolves crash
recovery. The fake provider's facts live in PostgreSQL
(``map_control.test_effect_facts``), so every asserted action count comes
from SERVER-SIDE PERSISTENT facts, never from an in-process list.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import MIGRATION_DSN
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    EFFECT_DELIVERED,
    EFFECT_DISPATCHING,
    EFFECT_UNCERTAIN,
    EffectLedger,
    Job,
    JobStatus,
    Message,
)
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


# ---------------------------------------------------------------------------
# Fake provider with SERVER-SIDE PERSISTENT facts (R4-P0-01).
# ---------------------------------------------------------------------------


async def _ensure_facts_table() -> None:
    """Create the fake-provider fact table with the MIGRATION role.

    Tables created by map_migrator in map_control inherit full DML grants
    for the app role (db/init/01-roles.sh default privileges), and the
    shared ``session`` fixture truncates them between tests like any other
    map_control table.
    """
    engine = create_async_engine(MIGRATION_DSN)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS map_control.test_effect_facts ("
                    " effect_key TEXT PRIMARY KEY,"
                    " outcome TEXT NOT NULL,"
                    " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
    finally:
        await engine.dispose()


class FakeEffectProvider:
    """EffectProvider whose facts are PostgreSQL rows.

    The fact row IS the external action: ``INSERT ... ON CONFLICT DO
    NOTHING`` makes the action structurally exactly-once per key — a
    repeated ``send()`` with the same key never executes a second action,
    it returns the stored outcome. All statistics below are read back from
    the database, never from an in-process list.
    """

    def __init__(self, factory, *, queryable: bool = True) -> None:
        self._factory = factory
        self._queryable = queryable
        self.send_calls = 0  # diagnostics only; never asserted as the fact

    async def send(self, key: str) -> bool:
        self.send_calls += 1
        async with self._factory() as session:
            await session.execute(
                text(
                    "INSERT INTO map_control.test_effect_facts (effect_key, outcome) "
                    "VALUES (:key, 'confirmed') ON CONFLICT (effect_key) DO NOTHING"
                ),
                {"key": key},
            )
            await session.commit()
        # First insert executed the action; a conflict means the action was
        # already performed under this key and is deduplicated. Either way
        # the stored acknowledgement is confirmed.
        return True

    async def query(self, key: str) -> bool | None:
        if not self._queryable:
            return None  # provider cannot confirm by key
        async with self._factory() as session:
            outcome = (
                await session.execute(
                    text(
                        "SELECT outcome FROM map_control.test_effect_facts "
                        "WHERE effect_key = :key"
                    ),
                    {"key": key},
                )
            ).scalar_one_or_none()
        if outcome is None:
            return False  # clearly never received
        return outcome == "confirmed"


async def fact_count(factory, key: str) -> int:
    """Server-side persistent action-fact count for ``key``."""
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM map_control.test_effect_facts "
                    "WHERE effect_key = :key"
                ),
                {"key": key},
            )
        ).scalar_one()


class _NeverCalledProvider:
    async def send(self, key: str) -> bool:  # pragma: no cover - must never run
        raise AssertionError("provider must not run without a valid key")

    async def query(self, key: str) -> bool | None:  # pragma: no cover
        raise AssertionError("provider must not run without a valid key")


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


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


def _guarded_handler(provider, crash_point: str | None, *, lease_seconds: float = 60.0):
    """One handler factory for every window; ``crash_point`` selects the
    simulated process death for the FIRST attempt of each round."""

    async def handler(job, session):
        ctx = get_current_job_context()
        guard = EffectGuard(ctx.session_factory)

        if crash_point == "before_intent":
            raise SimulatedCrash("killed before the intent could commit")

        if crash_point == "after_intent":
            await guard.record_intent(ctx.idempotency_key, job.workspace_id, job_id=job.id)
            raise SimulatedCrash("killed after intent commit, before dispatch")

        await guard.run_effect_once(
            ctx.idempotency_key,
            job.workspace_id,
            provider,
            job_id=job.id,
            dispatch_lease_seconds=lease_seconds,
        )
        if crash_point == "after_ack":
            raise SimulatedCrash("killed after ack commit, before job complete")
        return {"ok": True}

    return handler


def _patched_begin_dispatch_crash():
    """R4-P0-01 real PostgreSQL crash point: kill the process AFTER
    ``begin_dispatch()`` commits ``dispatching`` and BEFORE the provider
    callable's first line. The committed dispatch state survives in the
    database; the provider fact never exists for attempt 1."""
    original = EffectGuard.begin_dispatch

    async def crashing_begin(self, key, workspace_id, **kwargs):
        outcome = await original(self, key, workspace_id, **kwargs)
        if outcome.decision == "proceed":
            raise SimulatedCrash("kill -9 after dispatch commit, before provider call")
        return outcome

    return crashing_begin, original


async def _run_window(
    factory, window: str, crash_point: str | None, provider, *, lease_seconds: float = 60.0
) -> tuple[uuid.UUID, str]:
    """One round of window ``window``: attempt 1 crashes at ``crash_point``,
    attempt 2 runs to its natural outcome. Returns (job_id, key)."""
    key = f"{window}-{uuid.uuid4().hex[:12]}"
    job_id = await _create_job(factory, key=key)

    runner = JobRunner(
        factory,
        {"effect_test": _guarded_handler(provider, crash_point, lease_seconds=lease_seconds)},
        worker_id=f"{window}-p1",
        poll_seconds=0.05,
    )
    await runner.run_once()  # attempt 1 (crashes unless crash_point is None)
    await _reset_backoff(factory, job_id)

    # "Restart": fresh runner instance sharing only the database.
    runner2 = JobRunner(
        factory,
        {"effect_test": _guarded_handler(provider, None, lease_seconds=lease_seconds)},
        worker_id=f"{window}-p2",
        poll_seconds=0.05,
    )
    await runner2.run_once()  # attempt 2
    return job_id, key


# ---------------------------------------------------------------------------
# W1: crash before intent — retry performs the effect exactly once.
# ---------------------------------------------------------------------------


async def test_window1_crash_before_intent_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory)
    for rnd in range(ROUNDS):
        job_id, key = await _run_window(factory, "w1", "before_intent", provider)
        assert await fact_count(factory, key) == 1, f"round {rnd}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered"


# ---------------------------------------------------------------------------
# W2: intent committed, dispatch never started — retry proceeds (never skips).
# ---------------------------------------------------------------------------


async def test_window2_after_intent_before_dispatch_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory)
    for rnd in range(ROUNDS):
        job_id, key = await _run_window(factory, "w2", "after_intent", provider)
        assert await fact_count(factory, key) == 1, f"round {rnd}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered"


# ---------------------------------------------------------------------------
# W2b (R4-P0-01): dispatch committed, provider call never went out —
#     recovery queries the provider by key (never received), takes the
#     EXPIRED dispatch generation over by compare-and-set (R5-P0-01) and
#     re-sends with the SAME key: exactly one action, never the pre-R4
#     permanent loss (uncertain with zero actions).
#
#     The dispatch lease is deliberately short: a takeover is only legal
#     once the dead owner's lease expired in DATABASE time.
# ---------------------------------------------------------------------------

W2B_LEASE_S = 0.5


async def test_window2b_after_dispatch_before_provider_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory)
    crashing_begin, original_begin = _patched_begin_dispatch_crash()

    for rnd in range(ROUNDS):
        key = f"w2b-{uuid.uuid4().hex[:12]}"
        job_id = await _create_job(factory, key=key)

        EffectGuard.begin_dispatch = crashing_begin  # type: ignore[method-assign]
        try:
            runner = JobRunner(
                factory,
                {"effect_test": _guarded_handler(provider, None, lease_seconds=W2B_LEASE_S)},
                worker_id="w2b-p1",
                poll_seconds=0.05,
            )
            await runner.run_once()  # attempt 1: dies after dispatch commit
        finally:
            EffectGuard.begin_dispatch = original_begin  # type: ignore[method-assign]

        # The crash point is real: dispatching is durable in PostgreSQL and
        # the provider NEVER received the action (zero server-side facts).
        assert await _effect_state(factory, key) == "dispatching", f"round {rnd}"
        assert await fact_count(factory, key) == 0, f"round {rnd}"

        await _reset_backoff(factory, job_id)
        runner2 = JobRunner(
            factory,
            {"effect_test": _guarded_handler(provider, None, lease_seconds=W2B_LEASE_S)},
            worker_id="w2b-p2",
            poll_seconds=0.05,
        )
        await runner2.run_once()  # attempt 2: lease expired -> CAS -> re-send

        assert await fact_count(factory, key) == 1, f"round {rnd}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert job.attempt == 2, f"round {rnd}: {job.attempt}"
        assert await _effect_state(factory, key) == "delivered", f"round {rnd}"


# ---------------------------------------------------------------------------
# W3: provider action confirmed, ack never persisted.
#     - queryable provider: recovery confirms by key -> delivered, one action;
#     - non-queryable provider: fail closed -> uncertain + EFFECT_UNCERTAIN.
# ---------------------------------------------------------------------------


async def test_window3_after_action_before_ack_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory)

    async def crashing_ack(self, k, ws, **kwargs):
        raise SimulatedCrash("kill -9 between provider confirmation and ledger commit")

    original_ack = EffectGuard.ack_effect
    for rnd in range(ROUNDS):
        key = f"w3-{uuid.uuid4().hex[:12]}"
        job_id = await _create_job(factory, key=key)

        EffectGuard.ack_effect = crashing_ack  # type: ignore[method-assign]
        try:
            runner = JobRunner(
                factory,
                {"effect_test": _guarded_handler(provider, None)},
                worker_id="w3-p1",
                poll_seconds=0.05,
            )
            await runner.run_once()  # attempt 1: action done, ack lost
        finally:
            EffectGuard.ack_effect = original_ack  # type: ignore[method-assign]
        assert await fact_count(factory, key) == 1, f"round {rnd}"
        await _reset_backoff(factory, job_id)

        runner2 = JobRunner(
            factory,
            {"effect_test": _guarded_handler(provider, None)},
            worker_id="w3-p2",
            poll_seconds=0.05,
        )
        await runner2.run_once()  # attempt 2: query -> confirmed -> delivered

        # Confirmed by idempotency key: recovered to delivered, still ONE action.
        assert await fact_count(factory, key) == 1, f"round {rnd}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered", f"round {rnd}"


async def test_window3_non_queryable_provider_stays_uncertain(_engine, session) -> None:
    """Provider timeout/unknown that CANNOT be confirmed by key: the effect
    stays ``uncertain`` and the job is NEVER faked succeeded."""
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory, queryable=False)

    async def crashing_ack(self, k, ws, **kwargs):
        raise SimulatedCrash("kill -9 between provider confirmation and ledger commit")

    original_ack = EffectGuard.ack_effect
    # Short dispatch lease: recovery waits out the live-dispatch window
    # before failing closed (a live dispatcher might still complete).
    for rnd in range(5):
        key = f"w3nq-{uuid.uuid4().hex[:12]}"
        job_id = await _create_job(factory, key=key)

        EffectGuard.ack_effect = crashing_ack  # type: ignore[method-assign]
        try:
            runner = JobRunner(
                factory,
                {"effect_test": _guarded_handler(provider, None, lease_seconds=0.4)},
                worker_id="w3nq-p1",
                poll_seconds=0.05,
            )
            await runner.run_once()
        finally:
            EffectGuard.ack_effect = original_ack  # type: ignore[method-assign]
        await _reset_backoff(factory, job_id)

        runner2 = JobRunner(
            factory,
            {"effect_test": _guarded_handler(provider, None, lease_seconds=0.4)},
            worker_id="w3nq-p2",
            poll_seconds=0.05,
        )
        await runner2.run_once()

        assert await fact_count(factory, key) == 1, f"round {rnd}: action happened once"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.FAILED, f"round {rnd}: {job.status}"
        assert job.error_code == "EFFECT_UNCERTAIN", f"round {rnd}: {job.error_code}"
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
        await runner2.run_once()
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.FAILED
        assert job.error_code == "EFFECT_UNCERTAIN"
        assert await fact_count(factory, key) == 1, f"round {rnd}: never replayed"


# ---------------------------------------------------------------------------
# W4: confirmation committed, job completion lost — retry skips, succeeds.
# ---------------------------------------------------------------------------


async def test_window4_after_ack_before_job_complete_20_rounds(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory)
    for rnd in range(ROUNDS):
        job_id, key = await _run_window(factory, "w4", "after_ack", provider)
        assert await fact_count(factory, key) == 1, f"round {rnd}"
        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.SUCCEEDED, f"round {rnd}: {job.status}"
        assert await _effect_state(factory, key) == "delivered"


# ---------------------------------------------------------------------------
# Concurrency fence (R4-P0-01): two live runners, same key — exactly one
# external action, and the live dispatch is never prematurely uncertain.
# ---------------------------------------------------------------------------


async def test_concurrent_runners_same_key_single_action(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    provider = FakeEffectProvider(factory)
    key = f"conc-{uuid.uuid4().hex[:12]}"
    job_a = await _create_job(factory, key=key)
    job_b = await _create_job(factory, key=key)

    runner_a = JobRunner(
        factory,
        {"effect_test": _guarded_handler(provider, None)},
        worker_id="conc-a",
        poll_seconds=0.05,
    )
    runner_b = JobRunner(
        factory,
        {"effect_test": _guarded_handler(provider, None)},
        worker_id="conc-b",
        poll_seconds=0.05,
    )
    await asyncio.gather(runner_a.run_once(), runner_b.run_once())

    # Server-side fact: EXACTLY one external action for the shared key.
    assert await fact_count(factory, key) == 1
    assert await _effect_state(factory, key) == "delivered"
    for job_id in (job_a, job_b):
        job = await _job_row(factory, job_id)
        # Neither live call may be forced into uncertain by the other.
        assert job.error_code != "EFFECT_UNCERTAIN", f"{job_id}: {job.error_code}"
        assert job.status == JobStatus.SUCCEEDED, f"{job_id}: {job.status}"


# ---------------------------------------------------------------------------
# Dispatch generation fence (R5-P0-01): the minimal counter-examples of the
# fifth review, fixed as real-PostgreSQL proofs. A ``dispatching`` row is
# owned by ONE generation (dispatch_token + owner + attempt); the SQL
# ``WHERE`` clause — not documentation — enforces it.
# ---------------------------------------------------------------------------


async def test_live_dispatch_lease_is_never_adopted(_engine, session) -> None:
    """A's lease is ALIVE: B's compare-and-set must match nothing, so B never
    acquires the right to dispatch (no takeover without CAS)."""
    factory = _factory(_engine)
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)
    key = f"fence-live-{uuid.uuid4().hex[:8]}"

    await guard.record_intent(key, ws)
    a = await guard.begin_dispatch(key, ws, owner="A", attempt=1, lease_seconds=60.0)
    assert a.decision == "proceed"
    assert a.fence is not None

    observed = await guard.snapshot(key, ws)
    assert observed.status == EFFECT_DISPATCHING
    assert observed.token == a.fence.token
    assert observed.owner == "A"
    assert observed.lease_alive is True

    assert (
        await guard.adopt_dispatch(
            key, ws, owner="B", attempt=2, lease_seconds=60.0, observed=observed
        )
        is None
    )
    # The row is untouched: same generation, still dispatching.
    after = await guard.snapshot(key, ws)
    assert (after.status, after.token, after.owner, after.attempt) == (
        EFFECT_DISPATCHING,
        a.fence.token,
        "A",
        1,
    )


async def test_expired_lease_has_exactly_one_cas_winner_and_stale_writes_fail(
    _engine, session
) -> None:
    """After A's lease expires two recoveries race the CAS: exactly one wins,
    and A — now superseded — can neither ack nor terminalize the row, before
    OR after the winner settles it (the fifth review's stale-owner overwrite).
    """
    factory = _factory(_engine)
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)
    key = f"fence-cas-{uuid.uuid4().hex[:8]}"

    await guard.record_intent(key, ws)
    a = await guard.begin_dispatch(key, ws, owner="A", attempt=1, lease_seconds=0.2)
    assert a.fence is not None
    await asyncio.sleep(0.35)  # the lease expires in DATABASE time

    observed = await guard.snapshot(key, ws)
    assert observed.lease_alive is False

    # Two recoveries observe the SAME expired generation and race.
    fences = await asyncio.gather(
        guard.adopt_dispatch(key, ws, owner="B", attempt=2, lease_seconds=60.0, observed=observed),
        guard.adopt_dispatch(key, ws, owner="C", attempt=3, lease_seconds=60.0, observed=observed),
    )
    winners = [fence for fence in fences if fence is not None]
    assert len(winners) == 1, "exactly one CAS may win an expired generation"
    winner = winners[0]
    assert winner.token != a.fence.token, "a takeover always mints a NEW token"

    # A is superseded: both of its owner-sensitive writes must hit rowcount 0.
    assert await guard.mark_uncertain(key, ws, fence=a.fence, reason="stale owner") is False
    assert await guard.state_of(key, ws) == EFFECT_DISPATCHING
    assert await guard.ack_effect(key, ws, fence=a.fence) is False
    assert await guard.state_of(key, ws) == EFFECT_DISPATCHING

    # Only the current generation settles the effect.
    assert await guard.ack_effect(key, ws, fence=winner) is True
    assert await guard.state_of(key, ws) == EFFECT_DELIVERED

    # And a late stale write can never overwrite the terminal state either.
    assert await guard.mark_uncertain(key, ws, fence=a.fence, reason="stale after ack") is False
    assert await guard.state_of(key, ws) == EFFECT_DELIVERED


async def test_unknown_provider_never_terminalizes_a_live_dispatch(_engine, session) -> None:
    """``query -> None`` while another generation's lease is ALIVE: the
    resolver must write NOTHING (not even uncertain) and give up when its
    budget expires; only after the lease expired may the CURRENT observed
    generation fail closed."""
    factory = _factory(_engine)
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)
    key = f"fence-unknown-{uuid.uuid4().hex[:8]}"

    class _UnknownProvider:
        async def send(self, k: str) -> bool:  # pragma: no cover - must not run
            raise AssertionError("a live dispatch must never be taken over")

        async def query(self, k: str) -> bool | None:
            return None  # provider cannot confirm by key

    await guard.record_intent(key, ws)
    a = await guard.begin_dispatch(key, ws, owner="A", attempt=1, lease_seconds=1.0)
    assert a.fence is not None

    loop = asyncio.get_running_loop()
    outcome = await guard._resolve_dispatching(
        key,
        ws,
        _UnknownProvider(),
        owner="B",
        attempt=2,
        lease_seconds=60.0,
        deadline=loop.time() + 0.3,  # shorter than A's lease
    )
    assert outcome.decision == "timeout"
    live = await guard.snapshot(key, ws)
    assert (live.status, live.token, live.owner) == (EFFECT_DISPATCHING, a.fence.token, "A")

    # Once the lease expired the row may fail closed — under the taking-over
    # generation's own token, never under A's.
    await asyncio.sleep(1.0)
    outcome = await guard._resolve_dispatching(
        key,
        ws,
        _UnknownProvider(),
        owner="B",
        attempt=2,
        lease_seconds=60.0,
        deadline=loop.time() + 10.0,
    )
    assert outcome.decision == "uncertain"
    assert await guard.state_of(key, ws) == EFFECT_UNCERTAIN
    terminal = await guard.snapshot(key, ws)
    assert terminal.owner == "B"
    assert terminal.token != a.fence.token


async def test_confirm_provider_fact_is_monotonic(_engine, session) -> None:
    """The reconciliation path never revives a terminal ``uncertain`` row."""
    factory = _factory(_engine)
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)
    key = f"fence-mono-{uuid.uuid4().hex[:8]}"

    await guard.record_intent(key, ws)
    a = await guard.begin_dispatch(key, ws, owner="A", attempt=1, lease_seconds=60.0)
    assert a.fence is not None
    observed = await guard.snapshot(key, ws)
    assert await guard.mark_uncertain(key, ws, fence=a.fence, reason="failed closed") is True

    assert await guard.confirm_provider_fact(key, ws, observed=observed) is False
    assert await guard.state_of(key, ws) == EFFECT_UNCERTAIN


async def test_legacy_dispatching_row_without_token_is_adoptable_once(_engine, session) -> None:
    """Rows written BEFORE the R5-P0-01 migration carry NULL in every fence
    column (and no lease). NULL is a legitimate observed generation: it must
    be adoptable exactly once, and only the new token may settle the row."""
    factory = _factory(_engine)
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)
    key = f"fence-legacy-{uuid.uuid4().hex[:8]}"

    async with factory() as s:
        await s.execute(
            text(
                "INSERT INTO map_control.effect_ledger "
                "(workspace_id, effect_key, status, attempts) "
                "VALUES (:ws, :key, 'dispatching', 1)"
            ),
            {"ws": str(ws), "key": key},
        )
        await s.commit()

    observed = await guard.snapshot(key, ws)
    assert (observed.token, observed.owner, observed.attempt) == (None, None, None)
    assert observed.lease_alive is False, "a NULL lease counts as expired"

    fence = await guard.adopt_dispatch(
        key, ws, owner="B", attempt=1, lease_seconds=60.0, observed=observed
    )
    assert fence is not None
    # The same NULL generation can never be adopted twice.
    assert (
        await guard.adopt_dispatch(
            key, ws, owner="C", attempt=1, lease_seconds=60.0, observed=observed
        )
        is None
    )
    assert await guard.ack_effect(key, ws, fence=fence) is True
    assert await guard.state_of(key, ws) == EFFECT_DELIVERED


async def test_migrations_have_a_single_head(_engine) -> None:
    """The dispatch-token migration must not branch the revision graph."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    project_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "app/db/migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, script.get_heads()


# ---------------------------------------------------------------------------
# R5-P0-01 protocol-level barrier: the owner is INSIDE the provider call when
# its dispatch lease expires and a second worker legitimately takes the
# generation over. The late owner may claim NOTHING on its own; the run must
# still end with EXACTLY ONE external action and ledger=delivered — never
# ``uncertain``, and never "both attempts failed" (the fifth review's
# reproduced failure).
# ---------------------------------------------------------------------------

BARRIER_LEASE_S = 0.3
BARRIER_ROUNDS = 10


class _BarrierProvider:
    """Wraps the PostgreSQL-backed fake provider. ``hold`` keeps the caller
    INSIDE ``send()`` — exactly the window where its dispatch lease expires
    and another worker takes over; ``fail_late`` turns the late return into a
    provider timeout instead of a success."""

    def __init__(
        self,
        inner: FakeEffectProvider,
        *,
        entered: asyncio.Event | None = None,
        hold: asyncio.Event | None = None,
        fail_late: bool = False,
    ) -> None:
        self._inner = inner
        self._entered = entered
        self._hold = hold
        self._fail_late = fail_late

    async def send(self, key: str) -> bool:
        if self._entered is not None:
            self._entered.set()
        if self._hold is not None:
            await asyncio.wait_for(self._hold.wait(), timeout=30)
            if self._fail_late:
                raise ConnectionError("provider timed out after the lease expired")
        return await self._inner.send(key)

    async def query(self, key: str) -> bool | None:
        return await self._inner.query(key)


async def _run_barrier_round(guard, factory, ws, *, fail_late: bool) -> tuple[str, str, str]:
    """One barrier round; returns ``(key, a_result, b_result)``.

    A wins the dispatch and blocks inside ``send()`` until B — whose
    compare-and-set takeover only becomes legal once A's lease expired in
    database time — has completed the action and settled the ledger.
    """
    key = f"barrier-{uuid.uuid4().hex[:12]}"
    inner = FakeEffectProvider(factory)
    a_in_send = asyncio.Event()
    release_a = asyncio.Event()

    async def worker_a() -> str:
        return await guard.run_effect_once(
            key,
            ws,
            _BarrierProvider(inner, entered=a_in_send, hold=release_a, fail_late=fail_late),
            owner="barrier-A",
            attempt=1,
            dispatch_lease_seconds=BARRIER_LEASE_S,
        )

    async def worker_b() -> str:
        await a_in_send.wait()  # A owns the dispatch and is mid-call
        await asyncio.sleep(BARRIER_LEASE_S + 0.15)  # its lease expires
        return await guard.run_effect_once(
            key,
            ws,
            _BarrierProvider(inner),
            owner="barrier-B",
            attempt=2,
            dispatch_lease_seconds=60.0,
        )

    task_a = asyncio.create_task(worker_a())
    task_b = asyncio.create_task(worker_b())
    try:
        b_result = await task_b
    finally:
        # Only now does the superseded owner return from the provider.
        release_a.set()
    return key, await task_a, b_result


async def test_barrier_late_owner_after_legitimate_takeover(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()
    guard = EffectGuard(factory)
    ws = uuid.UUID(WORKSPACE)

    for late_kind in ("late_success", "late_timeout"):
        for rnd in range(BARRIER_ROUNDS):
            label = f"{late_kind} round {rnd}"
            key, a_result, b_result = await _run_barrier_round(
                guard, factory, ws, fail_late=late_kind == "late_timeout"
            )
            assert b_result == EFFECT_DELIVERED, label
            # The late owner claims nothing of its own: it reports delivered
            # only because the CONFIRMED fact is in the ledger.
            assert a_result == EFFECT_DELIVERED, label

            # ONE server-side action, and the terminal state belongs to the
            # generation that legitimately owned the row.
            assert await fact_count(factory, key) == 1, label
            final = await guard.snapshot(key, ws)
            assert final.status == EFFECT_DELIVERED, label
            assert (final.owner, final.attempt) == ("barrier-B", 2), label


# ---------------------------------------------------------------------------
# Provider unknown/timeout: never fake success.
# ---------------------------------------------------------------------------


async def test_provider_unknown_never_fakes_succeeded(_engine, session) -> None:
    factory = _factory(_engine)
    await _ensure_facts_table()

    for outcome in ("returned_false", "raised"):
        key = f"unknown-{uuid.uuid4().hex[:12]}"
        job_id = await _create_job(factory, key=key)

        class _UnknownProvider:
            def __init__(self, kind: str) -> None:
                self._kind = kind

            async def send(self, k: str) -> bool:
                if self._kind == "raised":
                    raise ConnectionError("provider timeout")
                return False  # unknown / unconfirmed

            async def query(self, k: str) -> bool | None:
                # No fact was ever persisted: the provider still cannot
                # CONFIRM delivery; the guard must fail closed.
                return None

        async def handler(job, session, _outcome=outcome):
            ctx = get_current_job_context()
            guard = EffectGuard(ctx.session_factory)
            await guard.run_effect_once(
                ctx.idempotency_key, job.workspace_id, _UnknownProvider(_outcome), job_id=job.id
            )
            return {"ok": True}

        runner = JobRunner(factory, {"effect_test": handler}, worker_id="w", poll_seconds=0.05)
        await runner.run_once()

        job = await _job_row(factory, job_id)
        assert job.status == JobStatus.FAILED, outcome
        assert job.error_code == "EFFECT_UNCERTAIN", outcome
        assert await _effect_state(factory, key) == EFFECT_UNCERTAIN
        assert await fact_count(factory, key) == 0

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
            await guard.run_effect_once(bad, ws, provider=_NeverCalledProvider())


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
