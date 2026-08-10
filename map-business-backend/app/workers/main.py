"""Worker process entry point.

Usage: ``python -m app.workers.main``

Graceful shutdown: SIGTERM/SIGINT stops claiming new jobs; the in-flight
handler finishes at its safe point before the process exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job
from ..db.session import get_session_factory
from .job_runner import JobHandler, JobRunner

logger = logging.getLogger(__name__)


def build_runner() -> JobRunner:
    # Handlers are registered here as job types are introduced. The
    # message_reconcile handler reconciles stale streaming messages after
    # a BFF crash; enqueue a job of this type per interval to trigger it.
    handlers: dict[str, JobHandler] = {
        "message_reconcile": _message_reconcile_handler,
    }
    raw_interval = os.getenv("MAP_WORKER_HEARTBEAT_INTERVAL_SECONDS", "").strip()
    return JobRunner(
        get_session_factory(),
        handlers=handlers,
        worker_id=os.getenv("MAP_WORKER_ID"),
        lease_seconds=int(os.getenv("MAP_WORKER_LEASE_SECONDS", "60")),
        poll_seconds=float(os.getenv("MAP_WORKER_POLL_SECONDS", "1.0")),
        # JobRunner validates at startup: any configured value must be
        # strictly below lease/3.
        heartbeat_interval_seconds=float(raw_interval) if raw_interval else None,
    )


async def _message_reconcile_handler(job: Job, session: AsyncSession) -> dict | None:
    """Reconcile stale streaming messages; idempotent and observable.

    R3-P0-01: runs on the runner-provided session and never commits on its
    own — the business writes ride the SAME transaction as the fenced
    ``complete()``; a lost lease rolls both back together.
    """
    from ..services.message_reconciler import reconcile_stale_streaming_messages
    from .job_runner import get_current_job_context

    ctx = get_current_job_context()
    if ctx is not None and not ctx.lease_ok:
        # Lease already lost before we started: produce no writes at all.
        return None
    stale_after_s = int(os.getenv("MAP_RECONCILE_STALE_AFTER_S", "300"))
    count = await reconcile_stale_streaming_messages(session, stale_after_s=stale_after_s)
    return {"reconciled": count}


async def _amain() -> None:
    runner = build_runner()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - windows
            signal.signal(sig, lambda *_: stop_event.set())

    logger.info("worker %s starting", runner.worker_id)
    await runner.run_forever(stop_event)
    logger.info("worker %s stopped gracefully", runner.worker_id)


def main() -> None:
    logging.basicConfig(level=os.getenv("MAP_LOG_LEVEL", "INFO"))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    main()
