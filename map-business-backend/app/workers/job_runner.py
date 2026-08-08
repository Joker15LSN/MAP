"""Background workers: durable job claim/heartbeat/retry (F-03).

Run with ``python -m app.workers.main`` as a separate process (compose
service). SIGTERM stops claiming new jobs and lets the in-flight handler
finish at its safe point.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Job
from ..repositories.jobs import JobRepository

JobHandler = Callable[[Job, AsyncSession], Awaitable[dict[str, Any] | None]]


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
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds

    async def run_once(self) -> bool:
        """Claim and execute at most one job. Returns True if one ran."""
        async with self._session_factory() as session:
            repo = JobRepository(session)
            job = await repo.claim_next(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
            if job is None:
                return False
            handler = self._handlers.get(job.job_type)
            if handler is None:
                await repo.fail(
                    job.id,
                    error_code="UNKNOWN_JOB_TYPE",
                    error_message=f"no handler registered for {job.job_type}",
                    retryable=False,
                )
                await session.commit()
                return True
            await self._execute(session, repo, job, handler)
            return True

    async def _execute(
        self,
        session: AsyncSession,
        repo: JobRepository,
        job: Job,
        handler: JobHandler,
    ) -> None:
        # Initial lease already set by claim_next; flush so the lease is
        # visible to other workers before we run the handler.
        await session.commit()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job.id, stop=asyncio.Event())
        )
        try:
            result = await handler(job, session)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            await session.rollback()
            await repo.fail(
                job.id,
                error_code="HANDLER_ERROR",
                error_message=str(exc)[:2000],
                retryable=True,
            )
            await session.commit()
            return
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        if result is None:
            return
        await repo.complete(job.id, result)
        await session.commit()

    async def _heartbeat_loop(self, job_id: uuid.UUID, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self.lease_seconds / 3)
            async with self._session_factory() as session:
                repo = JobRepository(session)
                if not await repo.heartbeat(job_id, lease_seconds=self.lease_seconds):
                    # Lease lost: another worker reclaimed the job. Stop
                    # heartbeating; the handler should stop at its safe point.
                    stop.set()
                    return

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Claim jobs until ``stop_event`` is set."""
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            ran = await self.run_once()
            if not ran:
                await asyncio.sleep(self.poll_seconds)
