"""Background workers: durable job claim/heartbeat/retry (F-03 / R2-P0-WORKER).

Run with ``python -m app.workers.main`` as a separate process (compose
service). SIGTERM stops claiming new jobs, signals the in-flight handler
(cancel event) and lets it finish at its safe point.

Lease safety:
- heartbeat runs in its own short transaction; a DB error marks the lease
  lost instead of silently running past expiry;
- heartbeat/complete/fail are fenced by ``lease_owner + attempt`` AND the
  live-lease condition ``lease_expires_at >= now()`` (database time), so a
  worker whose lease merely expired (before any reclaim) can never write a
  terminal state;
- heartbeat interval must be strictly below lease/3 (validated at
  construction);
- when complete/fail is rejected (ownership lost) the handler's session is
  explicitly rolled back: uncommitted handler DB writes never ride along;
- handlers observe ``get_current_job_context()`` for lease-lost/cancel and
  MUST check it before every side-effect safe point;
- external side effects go through :class:`EffectGuard`, a persisted
  effect ledger (``pending -> dispatching -> delivered | uncertain``).
  The ledger is honest about every crash window: an effect whose dispatch
  outcome is unknown becomes the observable terminal state ``uncertain``
  and the attached job fails with ``EFFECT_UNCERTAIN`` — it is never
  blindly replayed and never faked as succeeded.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import (
    EFFECT_DELIVERED,
    EFFECT_DISPATCHING,
    EFFECT_PENDING,
    EFFECT_UNCERTAIN,
    EffectLedger,
    Job,
)
from ..repositories.jobs import JobRepository

logger = logging.getLogger(__name__)

JobHandler = Callable[[Job, AsyncSession], Awaitable[dict[str, Any] | None]]


@dataclass
class JobExecutionContext:
    """Per-execution context handed to handlers via a context variable."""

    job_id: uuid.UUID
    workspace_id: uuid.UUID
    worker_id: str
    attempt: int
    lease_expires_at: Any
    idempotency_key: str | None
    lease_lost: asyncio.Event
    cancel: asyncio.Event
    session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def lease_ok(self) -> bool:
        return not self.lease_lost.is_set() and not self.cancel.is_set()


_current_ctx: ContextVar[JobExecutionContext | None] = ContextVar("map_job_ctx", default=None)


def get_current_job_context() -> JobExecutionContext | None:
    """Context for the handler currently executed by a :class:`JobRunner`.

    Handlers must check ``ctx.lease_ok`` before every side-effect safe
    point; once the lease is lost the handler must stop producing external
    effects immediately.
    """
    return _current_ctx.get()


class EffectUncertainError(Exception):
    """The external effect's outcome is unknown (provider timeout/unknown
    response, or a crash between dispatch and confirmation).

    The runner fails the job with ``EFFECT_UNCERTAIN`` (retryable=False):
    the effect must never be blindly replayed, and the job must never be
    reported as succeeded. The ledger row stays ``uncertain`` as the
    observable terminal state for operators.
    """


class EffectGuard:
    """Persisted effect ledger: provable at-most-once external effects.

    Handlers with external effects (network calls, payments, messages)
    run them through this guard, which persists one row per
    ``(workspace_id, effect_key)`` in ``map_control.effect_ledger``::

        pending -> dispatching -> delivered
                               or -> uncertain   (terminal, observable)

    Crash-window semantics (R3-P0-01):

    - crash BEFORE the intent commits: nothing durable; the retry records
      the intent and performs the effect (exactly one call);
    - crash AFTER intent, BEFORE the call: the row is ``pending`` — the
      effect may not have happened, so the retry PROCEEDS with the call
      (the old claim-based guard wrongly skipped here);
    - crash AFTER the call started, BEFORE confirmation: the row is
      ``dispatching`` — the outcome is unknown, recovery marks it
      ``uncertain`` and never replays (at-most-once, fail-closed);
    - crash AFTER confirmation, BEFORE job completion: the row is
      ``delivered`` — retries skip the call.

    Every transition is a fenced UPDATE committed in its own short
    transaction, so the ledger survives process restarts, retries,
    SIGTERM kills, handler rollbacks and lease takeovers.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _require_key(key: str | None) -> str:
        # R3-P0-01 acceptance: a side-effect job must carry a non-empty
        # idempotency key; None must never become a shared key across jobs.
        if not key or not str(key).strip():
            raise ValueError(
                "effect key must be a non-empty stable idempotency key; "
                "a missing key would be shared across jobs and can never "
                "deduplicate safely"
            )
        return str(key)

    async def record_intent(
        self, key: str, workspace_id: uuid.UUID, job_id: uuid.UUID | None = None
    ) -> None:
        """Persist the effect intent (idempotent upsert, own transaction).

        ``pending`` means "intent recorded, the external call may not have
        happened yet" — unlike the old claim it never authorizes skipping
        the call.
        """
        key = self._require_key(key)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(EffectLedger)
                .values(
                    workspace_id=workspace_id,
                    effect_key=key,
                    job_id=job_id,
                    status=EFFECT_PENDING,
                )
                .on_conflict_do_nothing(index_elements=["workspace_id", "effect_key"])
            )
            await session.execute(stmt)
            await session.commit()

    async def begin_dispatch(self, key: str, workspace_id: uuid.UUID) -> str:
        """Fenced transition authorizing (or refusing) the external call.

        Returns the resulting decision:

        - ``proceed``: ``pending -> dispatching`` committed; the caller
          owns the call now and MUST ack/mark afterwards;
        - ``delivered``: a previous attempt was confirmed; skip the call;
        - ``uncertain``: terminal (a previous dispatch crashed around the
          call, or the provider outcome was unknown); never call again.
        """
        key = self._require_key(key)
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status == EFFECT_PENDING,
                )
                .values(status=EFFECT_DISPATCHING, attempts=EffectLedger.attempts + 1)
            )
            if result.rowcount:
                await session.commit()
                return "proceed"
            await session.rollback()
            status = await self.state_of(key, workspace_id)
            if status == EFFECT_DELIVERED:
                return "delivered"
            if status == EFFECT_DISPATCHING:
                # A previous attempt crashed around the external call: the
                # outcome is unknown. Fail closed — never replay.
                await self.mark_uncertain(
                    key, workspace_id, reason="previous dispatch crashed before confirmation"
                )
            return "uncertain"

    async def ack_effect(self, key: str, workspace_id: uuid.UUID) -> bool:
        """Confirm the provider acknowledged the effect (dispatching ->
        delivered). Returns False when the fenced transition missed."""
        key = self._require_key(key)
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status == EFFECT_DISPATCHING,
                )
                .values(status=EFFECT_DELIVERED, last_outcome="confirmed")
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_uncertain(
        self, key: str, workspace_id: uuid.UUID, *, reason: str | None = None
    ) -> None:
        """Terminal state: the effect may or may not have happened."""
        key = self._require_key(key)
        async with self._session_factory() as session:
            await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status.in_([EFFECT_PENDING, EFFECT_DISPATCHING]),
                )
                .values(status=EFFECT_UNCERTAIN, last_outcome=(reason or "unknown")[:2000])
            )
            await session.commit()

    async def state_of(self, key: str, workspace_id: uuid.UUID) -> str | None:
        key = self._require_key(key)
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(EffectLedger.status).where(
                        EffectLedger.workspace_id == workspace_id,
                        EffectLedger.effect_key == key,
                    )
                )
            ).scalar_one_or_none()

    async def has_effect(self, key: str, workspace_id: uuid.UUID) -> bool:
        """True when the effect was CONFIRMED delivered (not merely claimed)."""
        return await self.state_of(key, workspace_id) == EFFECT_DELIVERED

    async def run_effect_once(
        self,
        key: str,
        workspace_id: uuid.UUID,
        provider: Callable[[], Awaitable[bool]],
        *,
        job_id: uuid.UUID | None = None,
    ) -> str:
        """Full guarded effect protocol; returns ``delivered`` or raises
        :class:`EffectUncertainError`.

        ``provider`` performs the external call and returns True only on a
        CONFIRMED acknowledgement; False (or any exception) means the
        outcome is unknown and the effect becomes ``uncertain`` — the job
        must never fake success on an unconfirmed provider response.
        """
        await self.record_intent(key, workspace_id, job_id=job_id)
        decision = await self.begin_dispatch(key, workspace_id)
        if decision == "delivered":
            return EFFECT_DELIVERED
        if decision == "uncertain":
            raise EffectUncertainError(
                f"effect {key!r} is in a terminal uncertain state; it must be "
                "resolved by an operator, never replayed"
            )
        try:
            confirmed = await provider()
        except Exception as exc:  # unknown outcome = uncertain, fail closed
            await self.mark_uncertain(key, workspace_id, reason=f"provider error: {exc}"[:2000])
            raise EffectUncertainError(
                f"effect {key!r}: provider raised before confirmation ({exc})"
            ) from exc
        if not confirmed:
            await self.mark_uncertain(
                key, workspace_id, reason="provider returned unknown/timeout"
            )
            raise EffectUncertainError(
                f"effect {key!r}: provider did not confirm (unknown/timeout)"
            )
        if not await self.ack_effect(key, workspace_id):
            # Concurrent terminal transition between call and ack: the
            # outcome is no longer ours to claim.
            raise EffectUncertainError(f"effect {key!r}: ack lost the fenced transition")
        return EFFECT_DELIVERED


class JobRunner:
    """Claims one job at a time per worker and dispatches to a handler."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: dict[str, JobHandler],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        poll_seconds: float = 1.0,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        # Keep the heartbeat interval strictly below lease/3 (default
        # lease/4). Any explicitly configured value is validated here: a
        # heartbeat at or above lease/3 cannot guarantee renewal before
        # expiry under one missed tick, so we fail fast at startup.
        if heartbeat_interval_seconds is None:
            self.heartbeat_interval_seconds = max(0.2, lease_seconds / 4.0)
        else:
            if heartbeat_interval_seconds <= 0:
                raise ValueError("heartbeat_interval_seconds must be positive")
            if heartbeat_interval_seconds >= lease_seconds / 3.0:
                raise ValueError(
                    f"heartbeat_interval_seconds={heartbeat_interval_seconds} must be "
                    f"strictly smaller than lease_seconds/3={lease_seconds / 3.0}"
                )
            self.heartbeat_interval_seconds = heartbeat_interval_seconds

    async def run_once(self, stop_event: asyncio.Event | None = None) -> bool:
        """Claim and execute at most one job. Returns True if one ran."""
        async with self._session_factory() as session:
            repo = JobRepository(session)
            job = await repo.claim_next(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
            if job is None:
                return False
            attempt = job.attempt or 0
            if job.job_type not in self._handlers:
                ok = await repo.fail(
                    job.id,
                    error_code="UNKNOWN_JOB_TYPE",
                    error_message=f"no handler registered for {job.job_type}",
                    retryable=False,
                    owner=self.worker_id,
                    attempt=attempt,
                )
                await session.commit()
                if not ok:
                    logger.warning(
                        "job fail ownership lost",
                        extra={
                            "job_id": str(job.id),
                            "worker_id": self.worker_id,
                            "attempt": attempt,
                        },
                    )
                return True
            # Initial lease is now committed and visible to other workers.
            await session.commit()
            await self._execute(session, repo, job, attempt, stop_event)
            return True

    async def _execute(
        self,
        session: AsyncSession,
        repo: JobRepository,
        job: Job,
        attempt: int,
        stop_event: asyncio.Event | None,
    ) -> None:
        ctx = JobExecutionContext(
            job_id=job.id,
            workspace_id=job.workspace_id,
            worker_id=self.worker_id,
            attempt=attempt,
            lease_expires_at=job.lease_expires_at,
            idempotency_key=job.idempotency_key,
            lease_lost=asyncio.Event(),
            cancel=asyncio.Event(),
            session_factory=self._session_factory,
        )
        token = _current_ctx.set(ctx)

        async def _watch_stop() -> None:
            if stop_event is None:
                return
            await stop_event.wait()
            ctx.cancel.set()

        stop_watcher = asyncio.create_task(_watch_stop())
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job, attempt, ctx))
        handler = self._handlers[job.job_type]
        try:
            result = await handler(job, session)
        except EffectUncertainError as exc:
            # The external effect's outcome is unknown: roll back the
            # handler's writes and fail TERMINALLY. Retrying would replay
            # nothing safely, and succeeding would fake an effect that was
            # never confirmed — ``uncertain`` is the observable state.
            await session.rollback()
            await repo.fail(
                job.id,
                error_code="EFFECT_UNCERTAIN",
                error_message=str(exc)[:2000],
                retryable=False,
                owner=self.worker_id,
                attempt=attempt,
            )
            await session.commit()
            logger.error(
                "job failed with uncertain external effect",
                extra={**self._log_fields(job, attempt), "error": str(exc)[:500]},
            )
            return
        except Exception as exc:  # noqa: BLE001 - worker boundary
            # Roll back every uncommitted handler write BEFORE the fenced
            # fail write; the two must never share a commit.
            await session.rollback()
            ok = await repo.fail(
                job.id,
                error_code="HANDLER_ERROR",
                error_message=str(exc)[:2000],
                retryable=True,
                owner=self.worker_id,
                attempt=attempt,
            )
            await session.commit()
            if not ok:
                # Ownership lost: the fenced UPDATE matched nothing, so the
                # commit above only closed our transaction; an eventual
                # reclaim owns the terminal state.
                await session.rollback()
                logger.warning(
                    "job fail rejected: ownership lost",
                    extra=self._log_fields(job, attempt),
                )
            return
        finally:
            import contextlib

            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            stop_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_watcher
            _current_ctx.reset(token)

        if ctx.cancel.is_set():
            # SIGTERM/stop reached the safe point: do not commit the
            # handler's partial writes; return the job to the queue.
            await session.rollback()
            ok = await repo.fail(
                job.id,
                error_code="WORKER_STOPPED",
                error_message="worker stopped before the job finished",
                retryable=True,
                owner=self.worker_id,
                attempt=attempt,
            )
            await session.commit()
            if not ok:
                await session.rollback()
            return

        if result is None or ctx.lease_lost.is_set():
            # Handler gave up (lease observed lost, or nothing to record).
            # Never commit handler writes without a live lease.
            await session.rollback()
            if ctx.lease_lost.is_set():
                logger.warning(
                    "handler finished after lease loss; writes rolled back",
                    extra=self._log_fields(job, attempt),
                )
            return

        # Fenced complete: the UPDATE itself re-checks owner/attempt/live
        # lease inside THIS transaction, so the lease check and the state
        # commit are atomic. On rejection, roll back the handler's writes.
        ok = await repo.complete(job.id, result, owner=self.worker_id, attempt=attempt)
        if not ok:
            await session.rollback()
            logger.warning(
                "job complete rejected: ownership lost or lease expired; "
                "handler writes rolled back",
                extra=self._log_fields(job, attempt),
            )
            return
        await session.commit()

    def _log_fields(self, job: Job, attempt: int) -> dict:
        # After rollback/commit the ORM instance is expired; never trigger a
        # lazy load here (logging runs outside the greenlet context).
        from sqlalchemy import inspect

        unloaded = inspect(job).unloaded

        def _value(name: str) -> str:
            if name in unloaded:
                return "<expired>"
            return str(getattr(job, name))

        return {
            "job_id": _value("id"),
            "workspace_id": _value("workspace_id"),
            "worker_id": self.worker_id,
            "attempt": attempt,
            "lease_expires_at": _value("lease_expires_at"),
        }

    async def _heartbeat_loop(self, job: Job, attempt: int, ctx: JobExecutionContext) -> None:
        while not ctx.lease_lost.is_set() and not ctx.cancel.is_set():
            # Jittered interval, always below lease/3.
            base = self.heartbeat_interval_seconds
            delay = base * (0.8 + random.random() * 0.4)
            await asyncio.sleep(delay)
            async with self._session_factory() as session:
                repo = JobRepository(session)
                try:
                    ok = await repo.heartbeat(
                        job.id,
                        lease_seconds=self.lease_seconds,
                        owner=self.worker_id,
                        attempt=attempt,
                    )
                    await session.commit()
                except Exception as exc:  # noqa: BLE001 - DB timeout etc.
                    import contextlib

                    with contextlib.suppress(Exception):
                        await session.rollback()
                    # A failed heartbeat must not silently run past the
                    # lease: treat it as lease lost (fail-closed).
                    logger.error(
                        "heartbeat failed, treating lease as lost",
                        extra={**self._log_fields(job, attempt), "error": str(exc)[:500]},
                    )
                    ok = False
            if not ok:
                ctx.lease_lost.set()
                logger.warning(
                    "lease lost for job",
                    extra=self._log_fields(job, attempt),
                )
                return

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Claim jobs until ``stop_event`` is set; then cancel the in-flight handler."""
        stop_event = stop_event or asyncio.Event()

        async def _watch() -> None:
            await stop_event.wait()
            ctx = _current_ctx.get()
            if ctx is not None:
                ctx.cancel.set()

        watcher = asyncio.create_task(_watch())
        try:
            while not stop_event.is_set():
                ran = await self.run_once(stop_event=stop_event)
                if not ran:
                    await asyncio.sleep(self.poll_seconds)
        finally:
            import contextlib

            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
