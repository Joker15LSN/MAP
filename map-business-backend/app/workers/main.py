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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    # R4-P1-01: the effect probe is an E2E-only handler (never registered
    # unless the fault matrix explicitly opts in) that drives the full
    # EffectGuard protocol against a server-side fact table, so a real
    # worker kill + takeover can be verified with a provider-side action
    # fact count.
    if os.getenv("MAP_E2E_EFFECT_PROBE", "").strip().lower() == "true":
        handlers["e2e_effect_probe"] = _e2e_effect_probe_handler
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
    # R4-P1-01 E2E-only crash-window barrier: when set (>0), the handler
    # holds here AFTER the claim (lease kept alive by the heartbeat) and
    # BEFORE any business write, so the fault matrix can kill exactly the
    # live lease owner at a deterministic in-flight point. Never set in
    # production/dev compose; default 0 = no behavior change.
    barrier_s = float(os.getenv("MAP_E2E_RECONCILE_BARRIER_S", "0") or 0)
    if barrier_s > 0:
        await asyncio.sleep(barrier_s)
        if ctx is not None and not ctx.lease_ok:
            return None
    stale_after_s = int(os.getenv("MAP_RECONCILE_STALE_AFTER_S", "300"))
    count = await reconcile_stale_streaming_messages(session, stale_after_s=stale_after_s)
    return {"reconciled": count}


class _E2eFactProvider:
    """E2E-only :class:`~app.workers.job_runner.EffectProvider`.

    The external action IS a row in ``map_control.e2e_effect_facts``
    (created by the E2E runner with explicit grants for the app role):
    ``INSERT ... ON CONFLICT DO NOTHING`` makes the action structurally
    exactly-once per idempotency key, and every fact the scenario asserts
    is read back from the SERVER, never from an in-process counter.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def send(self, idempotency_key: str) -> bool:
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO map_control.e2e_effect_facts (effect_key, outcome) "
                    "VALUES (:key, 'confirmed') ON CONFLICT (effect_key) DO NOTHING"
                ),
                {"key": idempotency_key},
            )
            await session.commit()
        return True

    async def query(self, idempotency_key: str) -> bool | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT outcome FROM map_control.e2e_effect_facts "
                        "WHERE effect_key = :key"
                    ),
                    {"key": idempotency_key},
                )
            ).scalar_one_or_none()
        if row is None:
            return False  # clearly never received
        return row == "confirmed"


async def _e2e_effect_probe_handler(job: Job, session: AsyncSession) -> dict | None:
    """R4-P1-01 E2E-only: run the full guarded effect protocol so the
    fault matrix can kill the real lease owner mid-flight and verify the
    provider-side action fact count stays EXACTLY 1 after takeover.

    The crash window is deterministic: the intent commits, then the
    optional ``MAP_E2E_EFFECT_BARRIER_S`` barrier holds the handler (lease
    kept alive by heartbeats) BEFORE dispatch/provider call — the exact
    window where a pre-R4 guard would lose or duplicate the action.
    """
    from .job_runner import EffectGuard, get_current_job_context

    ctx = get_current_job_context()
    guard = EffectGuard(ctx.session_factory)
    key = str((job.payload_json or {}).get("effect_key") or (ctx.idempotency_key or ""))
    await guard.record_intent(key, job.workspace_id, job_id=job.id)
    barrier_s = float(os.getenv("MAP_E2E_EFFECT_BARRIER_S", "0") or 0)
    if barrier_s > 0:
        await asyncio.sleep(barrier_s)
        if ctx is not None and not ctx.lease_ok:
            return None
    await guard.run_effect_once(
        key, job.workspace_id, _E2eFactProvider(ctx.session_factory), job_id=job.id
    )
    return {"effect": "delivered"}


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
