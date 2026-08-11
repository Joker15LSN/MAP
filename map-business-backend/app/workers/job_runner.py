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
  effect ledger (``pending -> dispatching -> delivered | uncertain``)
  combined with a provider-side idempotency contract (R4-P0-01). Every
  crash window is covered by durable facts, local AND provider-side:
  an effect whose outcome is provably unknown becomes the observable
  terminal state ``uncertain`` and the attached job fails with
  ``EFFECT_UNCERTAIN`` — it is never blindly replayed and never faked
  as succeeded; an effect whose outcome the provider can confirm is
  recovered to ``delivered`` with exactly one server-side action.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import func, or_, select, update
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
    response, or a crash between dispatch and confirmation that the
    provider cannot resolve by idempotency key).

    The runner fails the job with ``EFFECT_UNCERTAIN`` (retryable=False):
    the effect must never be blindly replayed, and the job must never be
    reported as succeeded. The ledger row stays ``uncertain`` as the
    observable terminal state for operators.
    """


@runtime_checkable
class EffectProvider(Protocol):
    """External-effect backend bound to a stable idempotency key (R4-P0-01).

    The key is FORCED through the interface: a provider can never be
    called without it, and a closure can never decide on its own whether
    to deduplicate. Implementations must persist call facts and
    confirmation results BY KEY on the provider/server side so recovery
    can distinguish "action confirmed", "action never received" and
    "unknown" across process deaths.
    """

    async def send(self, idempotency_key: str) -> bool:
        """Perform the external action deduplicated by ``idempotency_key``.

        Calling ``send`` again with the same key must NEVER produce a
        second external action; it returns the stored outcome instead.
        Returns True only on a CONFIRMED acknowledgement.
        """
        ...

    async def query(self, idempotency_key: str) -> bool | None:
        """Query the provider-side fact for ``idempotency_key``.

        True  = the action was confirmed delivered;
        False = the provider clearly never received the action;
        None  = unknown (cannot be determined).
        """
        ...


@dataclass(frozen=True)
class DispatchFence:
    """Exclusive right to dispatch ONE effect generation (R5-P0-01).

    ``token`` is minted per generation (``pending -> dispatching`` and every
    compare-and-set takeover) and is NEVER reused. Every owner-sensitive
    ledger UPDATE carries token + owner + attempt as its CAS predicate, so a
    holder that has been superseded observes ``rowcount = 0`` instead of
    overwriting the current generation.
    """

    token: uuid.UUID
    owner: str
    attempt: int


@dataclass(frozen=True)
class DispatchSnapshot:
    """One consistent read of a ledger row: state, the generation identity
    that owns it and whether its dispatch lease is still alive in DATABASE
    time (never a worker clock).

    Rows written before the R5-P0-01 migration carry NULL in every fence
    column: NULL is a valid observed generation (compared with
    ``IS NOT DISTINCT FROM``) whose lease counts as already expired.
    """

    status: str | None
    token: uuid.UUID | None
    owner: str | None
    attempt: int | None
    lease_alive: bool


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of asking the ledger for dispatch rights.

    ``decision`` is one of:

    - ``proceed``   - ``fence`` holds the exclusive dispatch generation;
    - ``delivered`` - the effect is confirmed; never call the provider;
    - ``uncertain`` - terminal, operator-owned; never replay;
    - ``dispatching`` - another generation owns the row; resolve it;
    - ``retry``     - the row is gone/reverted; re-record the intent.
    """

    decision: str
    fence: DispatchFence | None = None
    observed: DispatchSnapshot | None = None


@dataclass(frozen=True)
class _Settlement:
    """Outcome of one owned provider call: ``delivered``, ``uncertain`` (we
    committed the terminal state with our own fence) or ``lost-fence`` (a
    legitimate CAS takeover superseded us while we were inside the provider —
    we must NOT claim any terminal state and have to re-resolve)."""

    state: str
    reason: str = ""
    cause: BaseException | None = None


class EffectGuard:
    """Persisted effect ledger + provider-side idempotency: provable
    exactly-once external effects for providers implementing
    :class:`EffectProvider` (R4-P0-01).

    Handlers with external effects (network calls, payments, messages)
    run them through this guard, which persists one row per
    ``(workspace_id, effect_key)`` in ``map_control.effect_ledger``::

        pending -> dispatching -> delivered
                               or -> uncertain   (terminal, observable)

    Crash-window semantics (R3-P0-01 + R4-P0-01), resolved with BOTH the
    local ledger and the provider's server-side facts:

    - crash BEFORE the intent commits: nothing durable; the retry records
      the intent and performs the effect (exactly one action);
    - crash AFTER intent, BEFORE the dispatch transition: the row is
      ``pending`` — the retry proceeds with the call;
    - crash AFTER ``dispatching`` commits, BEFORE the provider call went
      out: recovery queries the provider by key — it clearly never
      received the action, so the row is re-adopted and re-sent with the
      SAME key (exactly one action; the pre-R4 implementation permanently
      lost this action by marking it ``uncertain``);
    - crash AFTER the provider action, BEFORE the ack commits: recovery
      queries the provider by key — confirmed, so the row is marked
      ``delivered`` without any second action; only when the provider
      CANNOT confirm (query -> unknown) does the row become ``uncertain``
      (fail closed, never faked succeeded);
    - crash AFTER confirmation, BEFORE job completion: the row is
      ``delivered`` — retries skip the call.

    Concurrency fence (R4-P0-01 + R5-P0-01): every ``dispatching`` row
    carries a generation identity — ``dispatch_token`` (non-reusable UUID)
    + ``dispatch_owner`` + ``dispatch_attempt`` — and a database-time
    ``dispatch_expires_at`` lease. The fence is not documentation: it is
    the SQL ``WHERE`` clause of every owner-sensitive UPDATE, and each of
    those methods returns whether its ``rowcount`` was 1:

    - ``begin_dispatch()``  mints a generation on ``pending -> dispatching``
      and returns the :class:`DispatchFence` that authorizes the call;
    - ``adopt_dispatch()``  is a single atomic CAS: it matches the OBSERVED
      token/owner/attempt AND requires the lease to be expired in database
      time, then mints a NEW token. A caller that loses it (rowcount 0) may
      not dispatch and must re-resolve the row's facts;
    - ``ack_effect()`` / ``mark_uncertain()`` only match the caller's OWN
      generation, so a stale worker can never terminalize (nor overwrite)
      the generation that superseded it;
    - ``confirm_provider_fact()`` is the single reconciliation path that may
      advance a non-terminal generation to ``delivered`` — it requires a
      CONFIRMED provider-side fact and matches the observed generation, so
      it can never race a stale ``uncertain`` into place (priority is
      strictly monotonic: a provider-confirmed ``delivered`` beats
      ``pending``/``dispatching``, and no path ever leaves ``uncertain``).

    A second caller therefore never marks a live dispatch ``uncertain``: it
    resolves the row from the provider's facts, and only after the dispatch
    lease expired without any provider confirmation does the effect fail
    closed — under the current generation's own token.

    Every ledger transition is a fenced UPDATE committed in its own short
    transaction, so the ledger survives process restarts, retries,
    SIGTERM kills, handler rollbacks and lease takeovers.
    """

    # Poll interval while another generation holds a live dispatch lease.
    _RESOLVE_POLL_S = 0.05

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

    @staticmethod
    def _fence_predicate(fence: DispatchFence) -> tuple[Any, ...]:
        """CAS predicate binding an UPDATE to exactly ONE generation.

        The token is minted per generation and never reused, so a superseded
        holder matches nothing and observes ``rowcount = 0``.
        """
        return (
            EffectLedger.dispatch_token == fence.token,
            EffectLedger.dispatch_owner == fence.owner,
            EffectLedger.dispatch_attempt == fence.attempt,
        )

    @staticmethod
    def _observed_predicate(observed: DispatchSnapshot) -> tuple[Any, ...]:
        """CAS predicate binding an UPDATE to the generation that was READ.

        NULL-safe on purpose: rows written before the R5-P0-01 migration have
        NULL in every fence column, and NULL is a legitimate observed
        generation that must still be adoptable exactly once.
        """
        return (
            EffectLedger.dispatch_token.is_not_distinct_from(observed.token),
            EffectLedger.dispatch_owner.is_not_distinct_from(observed.owner),
            EffectLedger.dispatch_attempt.is_not_distinct_from(observed.attempt),
        )

    async def begin_dispatch(
        self,
        key: str,
        workspace_id: uuid.UUID,
        *,
        owner: str,
        attempt: int,
        lease_seconds: float,
    ) -> DispatchOutcome:
        """Mint a dispatch generation on ``pending -> dispatching`` (R5-P0-01).

        Returns a :class:`DispatchOutcome`:

        - ``proceed``: the transition committed with a freshly minted
          ``dispatch_token`` + owner + attempt and a database-time lease;
          ``outcome.fence`` is the EXCLUSIVE right to call the provider and
          the only credential that can later ack/terminalize this row;
        - ``delivered``: a previous attempt was confirmed; skip the call;
        - ``uncertain``: terminal, operator-owned; never call again;
        - ``dispatching``: another generation owns the row — the caller must
          resolve it from the provider's server-side facts
          (see :meth:`_resolve_dispatching`) and may neither take over a live
          lease nor terminalize a generation it does not own;
        - ``retry``: the row is missing or back to ``pending`` (operator
          intervention); re-record the intent and retry the transition.
        """
        key = self._require_key(key)
        token = uuid.uuid4()
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status == EFFECT_PENDING,
                )
                .values(
                    status=EFFECT_DISPATCHING,
                    attempts=EffectLedger.attempts + 1,
                    dispatch_token=token,
                    dispatch_owner=owner,
                    dispatch_attempt=attempt,
                    dispatch_expires_at=func.now() + timedelta(seconds=lease_seconds),
                )
            )
            if result.rowcount:
                await session.commit()
                return DispatchOutcome(
                    "proceed",
                    fence=DispatchFence(token=token, owner=owner, attempt=attempt),
                )
            await session.rollback()
        observed = await self.snapshot(key, workspace_id)
        if observed.status == EFFECT_DELIVERED:
            return DispatchOutcome("delivered", observed=observed)
        if observed.status == EFFECT_UNCERTAIN:
            return DispatchOutcome("uncertain", observed=observed)
        if observed.status == EFFECT_DISPATCHING:
            return DispatchOutcome("dispatching", observed=observed)
        return DispatchOutcome("retry", observed=observed)

    async def adopt_dispatch(
        self,
        key: str,
        workspace_id: uuid.UUID,
        *,
        owner: str,
        attempt: int,
        lease_seconds: float,
        observed: DispatchSnapshot,
    ) -> DispatchFence | None:
        """Atomically take over an EXPIRED dispatch generation (R5-P0-01).

        ONE statement, ONE compare-and-set predicate: the row must still be
        ``dispatching`` carrying exactly the OBSERVED generation
        (token/owner/attempt, NULL-safe for pre-migration rows) AND its lease
        must already be expired in DATABASE time. Only then is a NEW token
        minted and returned.

        ``None`` means the CAS matched nothing (``rowcount = 0``): the caller
        never acquired the right to dispatch — it must re-read the facts and
        must not call the provider or write any terminal state.
        """
        key = self._require_key(key)
        token = uuid.uuid4()
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status == EFFECT_DISPATCHING,
                    *self._observed_predicate(observed),
                    # Database time only, and a NULL lease (legacy row) counts
                    # as expired so such rows stay recoverable.
                    or_(
                        EffectLedger.dispatch_expires_at.is_(None),
                        EffectLedger.dispatch_expires_at <= func.now(),
                    ),
                )
                .values(
                    attempts=EffectLedger.attempts + 1,
                    dispatch_token=token,
                    dispatch_owner=owner,
                    dispatch_attempt=attempt,
                    dispatch_expires_at=func.now() + timedelta(seconds=lease_seconds),
                )
            )
            if result.rowcount:
                await session.commit()
                return DispatchFence(token=token, owner=owner, attempt=attempt)
            await session.rollback()
        return None

    async def ack_effect(self, key: str, workspace_id: uuid.UUID, *, fence: DispatchFence) -> bool:
        """Confirm the provider acknowledged the effect (dispatching ->
        delivered) under the caller's OWN generation.

        Idempotent on the FACT, not on the fence: if our generation was
        superseded but the row is already ``delivered``, the confirmed
        delivery is what matters and this is a success. A ``False`` result
        means our fence is gone AND the row is not delivered — the outcome is
        no longer ours to claim.
        """
        key = self._require_key(key)
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status == EFFECT_DISPATCHING,
                    *self._fence_predicate(fence),
                )
                .values(status=EFFECT_DELIVERED, last_outcome="confirmed")
            )
            if result.rowcount:
                await session.commit()
                return True
            await session.rollback()
        return await self.state_of(key, workspace_id) == EFFECT_DELIVERED

    async def confirm_provider_fact(
        self, key: str, workspace_id: uuid.UUID, *, observed: DispatchSnapshot
    ) -> bool:
        """Reconcile a CONFIRMED provider-side fact into the ledger (R5-P0-01).

        The single path allowed to advance a generation the caller does NOT
        own, and it is deliberately narrow:

        - it requires the provider to have CONFIRMED the action by key;
        - it matches the OBSERVED generation, so it can never overwrite a
          takeover that happened after the read;
        - it is strictly monotonic: ``pending``/``dispatching`` ->
          ``delivered`` only. ``uncertain`` is never revived and
          ``delivered`` is never downgraded.

        ``False`` means the row moved under us and is not delivered: re-read
        the facts and resolve again.
        """
        key = self._require_key(key)
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status.in_([EFFECT_PENDING, EFFECT_DISPATCHING]),
                    *self._observed_predicate(observed),
                )
                .values(status=EFFECT_DELIVERED, last_outcome="confirmed-by-provider-query")
            )
            if result.rowcount:
                await session.commit()
                return True
            await session.rollback()
        return await self.state_of(key, workspace_id) == EFFECT_DELIVERED

    async def mark_uncertain(
        self,
        key: str,
        workspace_id: uuid.UUID,
        *,
        fence: DispatchFence,
        reason: str | None = None,
    ) -> bool:
        """Fail closed (dispatching -> uncertain) under the caller's OWN
        generation: the effect may or may not have happened.

        ``False`` (``rowcount = 0``) means the caller's generation was
        already superseded — a stale dispatcher must NEVER be able to turn
        another worker's live dispatch into a terminal ``uncertain``
        (R5-P0-01 minimal counter-example).
        """
        key = self._require_key(key)
        async with self._session_factory() as session:
            result = await session.execute(
                update(EffectLedger)
                .where(
                    EffectLedger.workspace_id == workspace_id,
                    EffectLedger.effect_key == key,
                    EffectLedger.status == EFFECT_DISPATCHING,
                    *self._fence_predicate(fence),
                )
                .values(status=EFFECT_UNCERTAIN, last_outcome=(reason or "unknown")[:2000])
            )
            if result.rowcount:
                await session.commit()
                return True
            await session.rollback()
        return False

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

    async def snapshot(self, key: str, workspace_id: uuid.UUID) -> DispatchSnapshot:
        """One consistent read of the row: status, the generation identity
        that owns it and whether its dispatch lease is still alive in
        DATABASE time (never worker clocks). A missing row and a NULL lease
        both read as "not alive"."""
        key = self._require_key(key)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        EffectLedger.status,
                        EffectLedger.dispatch_token,
                        EffectLedger.dispatch_owner,
                        EffectLedger.dispatch_attempt,
                        EffectLedger.dispatch_expires_at > func.now(),
                    ).where(
                        EffectLedger.workspace_id == workspace_id,
                        EffectLedger.effect_key == key,
                    )
                )
            ).one_or_none()
        if row is None:
            return DispatchSnapshot(None, None, None, None, False)
        return DispatchSnapshot(
            status=row[0],
            token=row[1],
            owner=row[2],
            attempt=row[3],
            lease_alive=bool(row[4]),
        )

    @staticmethod
    async def _provider_query(provider: EffectProvider, key: str) -> bool | None:
        """Provider-side fact lookup; a broken lookup degrades to unknown
        (fail closed), never to a false certainty."""
        try:
            return await provider.query(key)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            logger.warning("effect provider query failed for %r: %s", key, exc)
            return None

    async def _poll(self, deadline: float) -> bool:
        """Wait one poll interval; ``False`` when the resolve budget is spent."""
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(self._RESOLVE_POLL_S, remaining))
        return True

    async def _resolve_dispatching(
        self,
        key: str,
        workspace_id: uuid.UUID,
        provider: EffectProvider,
        *,
        owner: str,
        attempt: int,
        lease_seconds: float,
        deadline: float,
    ) -> DispatchOutcome:
        """Resolve a ``dispatching`` row from the provider's server-side facts
        (R4-P0-01) without ever violating the generation fence (R5-P0-01).

        The local row alone cannot distinguish "dispatch committed, call never
        went out" from "call went out, confirmation was lost"; the provider's
        per-key facts can. Every branch keeps two invariants:

        1. a generation whose lease is ALIVE is never taken over and never
           terminalized — the owner may still be inside the provider call, so
           we only poll (a provider-confirmed fact is still reconciled
           immediately, since ``delivered`` is monotonic and loses nothing);
        2. any action taken after the lease expired happens under a NEW token
           won by compare-and-set; losing that CAS means re-reading the facts,
           never proceeding on stale knowledge.

        Returns ``proceed`` (with an exclusive fence), ``delivered``,
        ``uncertain``, ``retry`` or ``timeout`` (budget spent while another
        generation held the row — nothing was written).
        """
        while True:
            observed = await self.snapshot(key, workspace_id)
            if observed.status == EFFECT_DELIVERED:
                return DispatchOutcome("delivered", observed=observed)
            if observed.status == EFFECT_UNCERTAIN:
                return DispatchOutcome("uncertain", observed=observed)
            if observed.status != EFFECT_DISPATCHING:
                # Missing row or reverted to pending (operator action): re-run
                # the normal transition instead of guessing.
                return DispatchOutcome("retry", observed=observed)

            outcome = await self._provider_query(provider, key)
            if outcome is True:
                # The action is a confirmed fact; reconcile it onto the
                # generation we read (monotonic, no takeover needed).
                if await self.confirm_provider_fact(key, workspace_id, observed=observed):
                    return DispatchOutcome("delivered", observed=observed)
                continue  # the row moved under us; re-read the facts

            if observed.lease_alive:
                # Another generation holds a LIVE lease. Whatever the provider
                # says right now (never received / unknown), that owner may
                # still be mid-call: we may neither adopt nor fail closed.
                if not await self._poll(deadline):
                    return DispatchOutcome("timeout", observed=observed)
                continue

            fence = await self.adopt_dispatch(
                key,
                workspace_id,
                owner=owner,
                attempt=attempt,
                lease_seconds=lease_seconds,
                observed=observed,
            )
            if fence is None:
                # Lost the CAS: somebody else adopted (or resolved) the row
                # first. We hold no rights — re-read before doing anything.
                continue

            # We now own an exclusive generation. Re-check the provider fact
            # (it may have landed during the takeover) and act under OUR token.
            outcome = await self._provider_query(provider, key)
            if outcome is True:
                if await self.ack_effect(key, workspace_id, fence=fence):
                    return DispatchOutcome("delivered", observed=observed)
                continue
            if outcome is False:
                # The provider clearly never received it: re-send with the
                # SAME key under our fence (exactly one external action).
                return DispatchOutcome("proceed", fence=fence, observed=observed)
            if await self.mark_uncertain(
                key,
                workspace_id,
                fence=fence,
                reason=(
                    "dispatch outcome unknown: provider cannot confirm by "
                    "idempotency key and the dispatch lease expired"
                ),
            ):
                return DispatchOutcome("uncertain", observed=observed)
            continue

    async def _settle_unconfirmed(
        self,
        key: str,
        workspace_id: uuid.UUID,
        provider: EffectProvider,
        *,
        fence: DispatchFence,
        reason: str,
        cause: BaseException | None = None,
    ) -> _Settlement:
        """The provider did not confirm: consult its per-key fact, then fail
        closed under our OWN fence. If the fence is gone a legitimate takeover
        superseded us mid-call — we must not write any terminal state and the
        caller has to re-resolve."""
        if await self._provider_query(provider, key) is True:
            if await self.ack_effect(key, workspace_id, fence=fence):
                return _Settlement("delivered")
            return _Settlement("lost-fence", reason="ack lost the dispatch fence", cause=cause)
        if await self.mark_uncertain(key, workspace_id, fence=fence, reason=reason[:2000]):
            return _Settlement("uncertain", reason=reason, cause=cause)
        return _Settlement(
            "lost-fence", reason="fail-closed write lost the dispatch fence", cause=cause
        )

    async def _dispatch_to_provider(
        self,
        key: str,
        workspace_id: uuid.UUID,
        provider: EffectProvider,
        *,
        fence: DispatchFence,
    ) -> _Settlement:
        """Perform the ONE provider call authorized by ``fence`` and settle it
        under the same fence (never under a generation we no longer own)."""
        try:
            confirmed = await provider.send(key)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            # The call may have partially succeeded; the provider's per-key
            # fact decides before we fail closed.
            return await self._settle_unconfirmed(
                key,
                workspace_id,
                provider,
                fence=fence,
                reason=f"provider error: {exc}",
                cause=exc,
            )
        if not confirmed:
            # Unconfirmed response (timeout/unknown): the provider may still
            # hold the fact; recover to delivered when it confirms.
            return await self._settle_unconfirmed(
                key,
                workspace_id,
                provider,
                fence=fence,
                reason="provider returned unknown/timeout",
            )
        if await self.ack_effect(key, workspace_id, fence=fence):
            return _Settlement("delivered")
        return _Settlement("lost-fence", reason="ack lost the dispatch fence")

    async def run_effect_once(
        self,
        key: str,
        workspace_id: uuid.UUID,
        provider: EffectProvider,
        *,
        job_id: uuid.UUID | None = None,
        owner: str | None = None,
        attempt: int | None = None,
        dispatch_lease_seconds: float = 60.0,
    ) -> str:
        """Full guarded effect protocol; returns ``delivered`` or raises
        :class:`EffectUncertainError`.

        ``provider`` implements :class:`EffectProvider`: the idempotency
        key is FORCED into ``send`` (the provider deduplicates by it
        server-side) and ``query`` resolves crash recovery by key. The
        provider returns True only on a CONFIRMED acknowledgement; when it
        cannot confirm, the guard consults ``query`` and otherwise fails
        closed to ``uncertain`` — the job must never fake success on an
        unconfirmed provider response.

        ``owner``/``attempt`` default to the current job context and, together
        with the per-generation ``dispatch_token``, fence every ledger write
        against concurrent callers (R5-P0-01); the dispatch lease must cover
        the expected provider call duration.

        The loop is bounded: takeovers only happen after a lease expires, so
        the budget is at least three lease windows (min 30s). Exhausting it
        raises without writing any terminal state — the row stays
        ``dispatching`` for its current owner instead of being stolen.
        """
        ctx = _current_ctx.get()
        if owner is None:
            owner = ctx.worker_id if ctx is not None else "unknown-worker"
        if attempt is None:
            attempt = ctx.attempt if ctx is not None else 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(30.0, dispatch_lease_seconds * 3)

        await self.record_intent(key, workspace_id, job_id=job_id)
        while True:
            outcome = await self.begin_dispatch(
                key,
                workspace_id,
                owner=owner,
                attempt=attempt,
                lease_seconds=dispatch_lease_seconds,
            )
            if outcome.decision == "dispatching":
                outcome = await self._resolve_dispatching(
                    key,
                    workspace_id,
                    provider,
                    owner=owner,
                    attempt=attempt,
                    lease_seconds=dispatch_lease_seconds,
                    deadline=deadline,
                )

            if outcome.decision == "delivered":
                return EFFECT_DELIVERED
            if outcome.decision == "uncertain":
                raise EffectUncertainError(
                    f"effect {key!r} is in the terminal uncertain state: the provider "
                    "cannot confirm the outcome by idempotency key. It must be resolved "
                    "by an operator, never replayed"
                )
            if outcome.decision == "timeout":
                raise EffectUncertainError(
                    f"effect {key!r}: another dispatch generation still holds the row "
                    "after the resolve budget expired; the outcome is unresolved and "
                    "this attempt refuses to steal or terminalize it"
                )
            if outcome.decision == "retry":
                if not await self._poll(deadline):
                    raise EffectUncertainError(
                        f"effect {key!r}: the ledger row kept reverting to a "
                        "non-dispatchable state within the resolve budget"
                    )
                await self.record_intent(key, workspace_id, job_id=job_id)
                continue

            fence = outcome.fence
            if fence is None:  # pragma: no cover - proceed always carries one
                raise RuntimeError("dispatch proceeded without a fence token")
            settlement = await self._dispatch_to_provider(key, workspace_id, provider, fence=fence)
            if settlement.state == "delivered":
                return EFFECT_DELIVERED
            if settlement.state == "uncertain":
                error = EffectUncertainError(f"effect {key!r}: {settlement.reason}")
                if settlement.cause is not None:
                    raise error from settlement.cause
                raise error
            # lost-fence: a legitimate takeover superseded us while we were
            # inside the provider. We may claim NOTHING; re-resolve the row so
            # the confirmed fact (if any) still becomes ``delivered``.
            logger.warning(
                "effect %r: %s (owner=%s attempt=%s); re-resolving",
                key,
                settlement.reason,
                fence.owner,
                fence.attempt,
            )
            if not await self._poll(deadline):
                raise EffectUncertainError(
                    f"effect {key!r}: dispatch fence was superseded and the outcome "
                    "could not be re-resolved within the budget"
                )


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
