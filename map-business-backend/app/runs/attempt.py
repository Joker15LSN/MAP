"""Run worker surface: claim, execute one attempt, settle.

The handler sees ONLY :class:`AttemptInput` and yields typed
:class:`CoreItem` values. It never learns about sessions, commits,
contextvars, lease safe points, exception classification or effect guard
ordering - all of that is hidden behind this module.

PR-C scope: cancel commands and terminal settlement are implemented; retry/
reconcile remain internal to later steps and are not exposed here.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

from ..runtime.state_machine import RunState
from .core_transport import CoreRunStream
from .domain import (
    AdvanceOutcome,
    AttemptInput,
    CoreError,
    CoreEvent,
    CoreItem,
    CoreOutcome,
    RunAttemptHandler,
    RunEventDraft,
)
from .errors import LeaseLostError
from .sandbox_effects import sandbox_invocation_handler
from .sandbox_remote import SandboxRemote
from .store import RunStore

_HANDLER_FORBIDDEN_PREFIXES = ("run.", "attempt.")
_DEFAULT_LEASE_SECONDS = 60


class AttemptAborted(Exception):
    """Worker shutdown asked the attempt to stop; nothing was written."""


class AttemptCancelled(Exception):
    """A durable cancel command was observed for this run mid-handler."""


def default_handler(core: CoreRunStream) -> RunAttemptHandler:
    """Built-in handler: drive core and relay its typed stream verbatim."""

    async def _drive(attempt: AttemptInput) -> AsyncIterator[CoreItem]:
        async for item in core.stream(attempt):
            yield item

    return _drive


class RunWorker:
    def __init__(
        self,
        store: RunStore,
        core: CoreRunStream,
        *,
        handler: RunAttemptHandler | None = None,
        sandbox_remote: SandboxRemote | None = None,
        worker_id: str | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._store = store
        self._core = core
        self._sandbox_remote = sandbox_remote
        self._handler = handler
        self.worker_id = worker_id or f"run-worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds

    def _default_handler_for(self, kind: str) -> RunAttemptHandler:
        if kind == "conversation_turn":
            return default_handler(self._core)
        if kind == "sandbox_invocation":
            return sandbox_invocation_handler(self._sandbox_remote)
        raise ValueError(f"unsupported RunCommand kind: {kind!r}")

    async def claim_and_run(self, *, worker_id: str) -> AdvanceOutcome | None:
        return await self.run_once(worker_id=worker_id)

    async def run_once(
        self, *, worker_id: str, stop_event: asyncio.Event | None = None
    ) -> AdvanceOutcome | None:
        claim = await self._store.claim_next(
            worker_id=worker_id, lease_seconds=self.lease_seconds
        )
        if claim is None:
            return None
        stop_event = stop_event or asyncio.Event()
        view = await self._store.get_run_view(
            workspace_id=claim.workspace_id,
            principal_id=claim.principal_id,
            run_id=claim.run_id,
        )
        if view is None:
            raise LeaseLostError(str(claim.run_id), claim.attempt)

        # BFF writes cancel COMMANDS only; the worker owns the transition.
        if view.cancel_requested:
            await self._store.settle_terminal(
                claim=claim,
                event_type="run.cancelling",
                data={"reason": "cancel requested"},
            )
            await self._store.settle_terminal(
                claim=claim, event_type="run.cancelled", data={}
            )
            return AdvanceOutcome(
                run_id=claim.run_id,
                attempt=claim.attempt,
                run_status=RunState.CANCELLED,
                events_appended=2,
            )

        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(claim, lease_lost, stop_event)
        )
        events_appended = 0
        try:
            if view.status == RunState.QUEUED:
                await self._store.settle_terminal(
                    claim=claim, event_type="run.started", data={}
                )
                events_appended += 1
            await self._store.append_events(
                claim=claim,
                drafts=[
                    RunEventDraft(
                        type="attempt.started",
                        data={"attempt": claim.attempt},
                    )
                ],
            )
            events_appended += 1

            attempt_input = AttemptInput(
                run_id=claim.run_id,
                workspace_id=claim.workspace_id,
                attempt=claim.attempt,
                command=claim.command,
            )
            outcome = await self._drive_handler(
                attempt_input, claim, lease_lost, stop_event
            )
            events_appended += outcome.events_appended
            return AdvanceOutcome(
                run_id=claim.run_id,
                attempt=claim.attempt,
                run_status=outcome.run_status,
                events_appended=events_appended,
                attempt_retryable=outcome.attempt_retryable,
            )
        except AttemptCancelled:
            # The handler has already been aclosed by _drive_handler; the
            # worker owns the transition and settles the cancel command it
            # observed. stop/done races converge through the existing CAS
            # in settle_terminal.
            await self._store.settle_terminal(
                claim=claim,
                event_type="run.cancelling",
                data={"reason": "cancel requested"},
            )
            await self._store.settle_terminal(
                claim=claim, event_type="run.cancelled", data={}
            )
            return AdvanceOutcome(
                run_id=claim.run_id,
                attempt=claim.attempt,
                run_status=RunState.CANCELLED,
                events_appended=2,
            )
        finally:
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def run_forever(
        self, *, worker_id: str, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            with contextlib.suppress(LeaseLostError, AttemptAborted):
                # Nothing was written by the loser; the lease owner / next
                # worker reconciles from durable facts.
                await self.run_once(worker_id=worker_id, stop_event=stop_event)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                return
            except TimeoutError:
                continue

    async def _heartbeat_loop(
        self,
        claim,
        lease_lost: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        interval = max(1.0, claim.lease_seconds / 3.0)
        while not stop_event.is_set() and not lease_lost.is_set():
            await asyncio.sleep(interval)
            if stop_event.is_set() or lease_lost.is_set():
                return
            ok = await self._store.heartbeat(
                claim=claim, lease_seconds=claim.lease_seconds
            )
            if not ok:
                lease_lost.set()
                return

    async def _cancel_watch_loop(
        self,
        claim,
        cancel_requested: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """Poll the durable cancel-command fact (~1s) during a handler.

        The cancel command is BFF-owned; the worker owns the transition.
        A transient store error is retried on the next tick (the lease
        heartbeat is the fail-safe for a truly broken connection).
        """
        try:
            while not stop_event.is_set() and not cancel_requested.is_set():
                await asyncio.sleep(1.0)
                if stop_event.is_set() or cancel_requested.is_set():
                    return
                try:
                    requested = await self._store.has_cancel_request(claim=claim)
                except Exception:  # noqa: BLE001 - transient poll failure
                    continue
                if requested:
                    cancel_requested.set()
                    return
        except asyncio.CancelledError:
            raise

    async def _drive_handler(
        self,
        attempt: AttemptInput,
        claim,
        lease_lost: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> AdvanceOutcome:
        events_appended = 0
        terminal: CoreOutcome | CoreError | None = None
        handler = self._handler or self._default_handler_for(attempt.command.kind)
        agen = handler(attempt)
        cancel_requested = asyncio.Event()
        cancel_watch_task = asyncio.create_task(
            self._cancel_watch_loop(claim, cancel_requested, stop_event)
        )
        try:
            while True:
                item_task = asyncio.create_task(anext(agen))
                stop_task = asyncio.create_task(stop_event.wait())
                lease_task = asyncio.create_task(lease_lost.wait())
                cancel_task = asyncio.create_task(cancel_requested.wait())
                done, pending = await asyncio.wait(
                    {item_task, stop_task, lease_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_task in done:
                    raise AttemptAborted("worker stopping")
                if lease_task in done:
                    raise LeaseLostError(str(claim.run_id), claim.attempt)
                if cancel_task in done:
                    raise AttemptCancelled(str(claim.run_id))
                try:
                    item = item_task.result()
                except StopAsyncIteration:
                    break
                self._check_running(claim, lease_lost, stop_event)
                if isinstance(item, CoreEvent):
                    await self._append_handler_event(claim, item)
                    events_appended += 1
                elif isinstance(item, (CoreOutcome, CoreError)):
                    terminal = item
                    break
                else:
                    raise TypeError(f"unsupported CoreItem: {type(item).__name__}")
            if terminal is None:
                raise RuntimeError("attempt handler ended without an outcome")
        except asyncio.CancelledError:
            raise
        except (LeaseLostError, AttemptAborted, AttemptCancelled):
            raise
        except Exception as exc:  # noqa: BLE001 - runner owns classification
            await self._store.append_events(
                claim=claim,
                drafts=[
                    RunEventDraft(
                        type="attempt.failed",
                        data={"attempt": claim.attempt, "error": str(exc)},
                    )
                ],
            )
            events_appended += 1
            scheduled = await self._store.fail_attempt(
                claim=claim,
                error_code="HANDLER_ERROR",
                error_message=str(exc),
                retryable=True,
            )
            if scheduled:
                return AdvanceOutcome(
                    run_id=claim.run_id,
                    attempt=claim.attempt,
                    run_status=RunState.RUNNING,
                    events_appended=events_appended,
                    attempt_retryable=True,
                )
            await self._store.settle_terminal(
                claim=claim,
                event_type="run.failed",
                data={"code": "HANDLER_ERROR", "message": str(exc)},
            )
            events_appended += 1
            return AdvanceOutcome(
                run_id=claim.run_id,
                attempt=claim.attempt,
                run_status=RunState.FAILED,
                events_appended=events_appended,
                attempt_retryable=False,
            )
        finally:
            if not cancel_watch_task.done():
                cancel_watch_task.cancel()
            await asyncio.gather(cancel_watch_task, return_exceptions=True)
            # Cancellation/stop propagates INTO the handler stream through
            # aclose(); its finally blocks must release the core connection.
            await agen.aclose()

        await self._store.append_events(
            claim=claim,
            drafts=[
                RunEventDraft(
                    type="attempt.completed"
                    if isinstance(terminal, CoreOutcome)
                    and terminal.status == "completed"
                    else "attempt.failed",
                    data={"attempt": claim.attempt},
                )
            ],
        )
        events_appended += 1
        if isinstance(terminal, CoreOutcome) and terminal.status == "completed":
            await self._store.settle_terminal(
                claim=claim, event_type="run.completed", data={}
            )
            events_appended += 1
            return AdvanceOutcome(
                run_id=claim.run_id,
                attempt=claim.attempt,
                run_status=RunState.COMPLETED,
                events_appended=events_appended,
            )
        if isinstance(terminal, CoreOutcome):
            code = terminal.error_code or "CORE_FAILED"
            message = terminal.error_message or "core outcome failed"
            retryable = False
        else:
            code = terminal.code
            message = terminal.message
            retryable = True  # transport failure: the attempt can be retried
        scheduled = await self._store.fail_attempt(
            claim=claim,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )
        if scheduled:
            return AdvanceOutcome(
                run_id=claim.run_id,
                attempt=claim.attempt,
                run_status=RunState.RUNNING,
                events_appended=events_appended,
                attempt_retryable=True,
            )
        await self._store.settle_terminal(
            claim=claim,
            event_type="run.failed",
            data={"code": code, "message": message},
        )
        events_appended += 1
        return AdvanceOutcome(
            run_id=claim.run_id,
            attempt=claim.attempt,
            run_status=RunState.FAILED,
            events_appended=events_appended,
            attempt_retryable=False,
        )

    async def _append_handler_event(self, claim, item: CoreEvent) -> None:
        if item.type.startswith(_HANDLER_FORBIDDEN_PREFIXES):
            raise ValueError(
                f"handler yielded reserved event type {item.type!r}; "
                "run.*/attempt.* are owned by the Run module"
            )
        await self._store.append_events(
            claim=claim,
            drafts=[RunEventDraft(type=item.type, data=item.data)],
        )

    @staticmethod
    def _check_running(
        claim, lease_lost: asyncio.Event, stop_event: asyncio.Event
    ) -> None:
        if stop_event.is_set():
            raise AttemptAborted("worker stopping")
        if lease_lost.is_set():
            raise LeaseLostError(str(claim.run_id), claim.attempt)
