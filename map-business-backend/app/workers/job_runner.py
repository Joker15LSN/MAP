"""Background workers: durable job claim/heartbeat/retry (F-03 / FIX-P0-WORKER-01).

Run with ``python -m app.workers.main`` as a separate process (compose
service). SIGTERM stops claiming new jobs, signals the in-flight handler
(cancel event) and lets it finish at its safe point.

Lease safety:
- heartbeat runs in its own short transaction; a DB error marks the lease
  lost instead of silently running past expiry;
- heartbeat/complete/fail are fenced by ``lease_owner + attempt``;
- heartbeat interval < lease/3 with jitter;
- handlers observe ``get_current_job_context()`` for lease-lost/cancel and
  MUST check it before every side-effect safe point.
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

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Job
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

    @property
    def lease_ok(self) -> bool:
        return not self.lease_lost.is_set() and not self.cancel.is_set()


_current_ctx: ContextVar[JobExecutionContext | None] = ContextVar(
    "map_job_ctx", default=None
)


def get_current_job_context() -> JobExecutionContext | None:
    """Context for the handler currently executed by a :class:`JobRunner`.

    Handlers must check ``ctx.lease_ok`` before every side-effect safe
    point; once the lease is lost the handler must stop producing external
    effects immediately.
    """
    return _current_ctx.get()


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
        # Keep the heartbeat interval below lease/3 (default lease/4).
        self.heartbeat_interval_seconds = (
            heartbeat_interval_seconds
            if heartbeat_interval_seconds is not None
            else max(0.2, lease_seconds / 4.0)
        )

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
        )
        token = _current_ctx.set(ctx)

        async def _watch_stop() -> None:
            if stop_event is None:
                return
            await stop_event.wait()
            ctx.cancel.set()

        stop_watcher = asyncio.create_task(_watch_stop())
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job, attempt, ctx)
        )
        handler = self._handlers[job.job_type]
        try:
            result = await handler(job, session)
        except Exception as exc:  # noqa: BLE001 - worker boundary
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

        if result is None:
            return
        ok = await repo.complete(job.id, result, owner=self.worker_id, attempt=attempt)
        await session.commit()
        if not ok:
            logger.warning(
                "job complete rejected: ownership lost",
                extra=self._log_fields(job, attempt),
            )

    def _log_fields(self, job: Job, attempt: int) -> dict:
        return {
            "job_id": str(job.id),
            "workspace_id": str(job.workspace_id),
            "worker_id": self.worker_id,
            "attempt": attempt,
            "lease_expires_at": str(job.lease_expires_at),
        }

    async def _heartbeat_loop(
        self, job: Job, attempt: int, ctx: JobExecutionContext
    ) -> None:
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
