"""Worker process entry point.

Usage: ``python -m app.workers.main``

Graceful shutdown: SIGTERM/SIGINT stops claiming new jobs; the in-flight
handler finishes at its safe point before the process exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from ..db.session import get_session_factory
from .job_runner import JobRunner

logger = logging.getLogger(__name__)


def build_runner() -> JobRunner:
    # Handlers are registered here as job types are introduced (R1-EVAL,
    # R2-NOTIFY, ...). For now the runner supports no built-in types and
    # simply waits; tests register their own handlers on JobRunner.
    return JobRunner(
        get_session_factory(),
        handlers={},
        worker_id=os.getenv("MAP_WORKER_ID"),
        lease_seconds=int(os.getenv("MAP_WORKER_LEASE_SECONDS", "60")),
        poll_seconds=float(os.getenv("MAP_WORKER_POLL_SECONDS", "1.0")),
    )


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
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
