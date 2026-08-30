"""Step 8 PR-K1: minimal typed execution event schema and emitter skeleton.

Design A, frozen by the run.md contract work:

- ``CoreExecutionEvent`` is the single typed event model for the NDJSON
  stream.  Construction validates canonical JSON serializability, the 64KiB
  inline ``data`` limit and ``seq >= 1``.
- ``RunContext`` carries the durable run identity through a contextvar.
  ``ExecutionEventEmitter`` snapshots it at emit time, so event code never
  passes ``run_id``/``attempt``/``workspace_id`` around manually.
- The emitter owns one bounded asyncio.Queue + one worker task per run
  context.  ``emit`` is synchronous (it only enqueues); ``drain``/``close``
  await delivery.  When the queue is full the event is dropped with a
  warning, matching the legacy async dispatcher queue-full semantics.

This module is core plumbing only.  K3/K4 will register real sinks and
replace the legacy ``GlobalAgentStateStore`` callers; nothing in this
commit changes production call sites.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..observability import current_trace_context
from ..utils.serialization import safe_serialize

INLINE_PAYLOAD_MAX_BYTES: int = 65536

# Frozen event type set for the Core typed stream (Step 8 PR-K1).  K5 adds
# the NDJSON router on top of these types.
ExecutionEventType = Literal[
    "step.started",
    "step.completed",
    "step.failed",
    "message.delta",
    "tool.invocation_created",
    "tool.invocation_completed",
    "tool.invocation_failed",
    "model.invocation_created",
    "model.invocation_sent",
    "model.invocation_succeeded",
    "model.invocation_failed",
    "model.invocation_unknown",
    "checkpoint.written",
    "effect.planned",
    "effect.executing",
    "effect.succeeded",
    "effect.failed",
    "effect.uncertain",
    "effect.reconciling",
    "effect.reconciled",
    "effect.cancelled",
    "stream.terminal",
]

_STREAM_TERMINAL_KEYS: frozenset[str] = frozenset(
    {"status", "error_code", "error_message"}
)

_JSON_SCALARS = (str, int, float, bool, type(None))


class ExecutionEventError(ValueError):
    """Typed construction/serialization error for Core execution events."""


class RunContextUnavailableError(RuntimeError):
    """Raised when an emit is attempted outside a RunContext."""


def _validate_json_types(value: Any, path: str = "$") -> None:
    """Reject every non-canonical-JSON value inside ``data``.

    NaN/Infinity, bytes, sets and arbitrary objects fail here, before the
    event reaches any sink or the NDJSON stream.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExecutionEventError(
                    f"{path}: object keys must be strings, got "
                    f"{type(key).__name__}"
                )
            _validate_json_types(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_types(item, f"{path}[{index}]")
        return
    if isinstance(value, _JSON_SCALARS):
        if isinstance(value, float) and not math.isfinite(value):
            raise ExecutionEventError(f"{path}: non-finite float is not JSON")
        return
    raise ExecutionEventError(
        f"{path}: {type(value).__name__} is not canonical JSON"
    )


def _canonical_json_bytes(payload: Any) -> bytes:
    """Canonical JSON UTF-8 bytes; NaN/Infinity/non-JSON values raise."""
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionEventError(
            f"data is not canonical JSON serializable: {exc}"
        ) from exc
    return text.encode("utf-8")


class CoreExecutionEvent(BaseModel):
    """Typed event carried by the Core NDJSON stream (schema version 1)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_id: uuid.UUID
    run_id: uuid.UUID
    attempt: int
    seq: int
    type: ExecutionEventType
    occurred_at: datetime
    workspace_id: uuid.UUID | None = None
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("seq", mode="before")
    @classmethod
    def _reject_bool_seq(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(f"seq must be an integer, got {value!r}")
        return value

    @field_validator("seq")
    @classmethod
    def _validate_seq(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"seq must be an integer, got {value!r}")
        if value < 1:
            raise ValueError("seq must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_data_contract(self) -> CoreExecutionEvent:
        _validate_json_types(self.data)
        size = len(_canonical_json_bytes(self.data))
        if size > INLINE_PAYLOAD_MAX_BYTES:
            raise ExecutionEventError(
                f"data is {size} bytes; inline limit is "
                f"{INLINE_PAYLOAD_MAX_BYTES} bytes"
            )
        if self.type == "stream.terminal":
            missing = _STREAM_TERMINAL_KEYS.difference(self.data)
            if missing:
                raise ExecutionEventError(
                    "stream.terminal data must contain "
                    f"{sorted(_STREAM_TERMINAL_KEYS)}; missing {sorted(missing)}"
                )
        return self


@dataclass(frozen=True)
class RunContext:
    """Durable run identity frozen at the request boundary."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID | None = None
    attempt: int = 1
    request_id: str | None = None
    session_id: str | None = None
    staff_code: str | None = None


current_run_context: ContextVar[RunContext | None] = ContextVar(
    "map_execution_run_context", default=None
)


def coerce_uuid(value: str | None, *, namespace: str = "map") -> uuid.UUID | None:
    """Coerce an F-04 identity header value to a stable UUID.

    Standard UUID strings (with or without hyphens) keep their value;
    arbitrary header strings are namespaced into a deterministic UUID so
    RunContext identities remain typed without losing traceability.
    """
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        pass
    try:
        return uuid.UUID(hex=value)
    except (ValueError, AttributeError):
        pass
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{value}")


@contextmanager
def set_run_context(
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
    attempt: int = 1,
    request_id: str | None = None,
    session_id: str | None = None,
    staff_code: str | None = None,
) -> Iterator[None]:
    """Run an async/sync block with the given RunContext installed."""
    token = current_run_context.set(
        RunContext(
            run_id=run_id,
            workspace_id=workspace_id,
            attempt=attempt,
            request_id=request_id,
            session_id=session_id,
            staff_code=staff_code,
        )
    )
    try:
        yield
    finally:
        current_run_context.reset(token)


class ExecutionEventSink(Protocol):
    """Duck-type contract every typed-event sink must implement."""

    async def emit(self, event: CoreExecutionEvent) -> None: ...

    async def aclose(self) -> None: ...


class InMemoryExecutionEventSink:
    """List + lock sink for tests and golden harnesses."""

    def __init__(self) -> None:
        self.events: list[CoreExecutionEvent] = []
        self._lock = asyncio.Lock()

    async def emit(self, event: CoreExecutionEvent) -> None:
        async with self._lock:
            self.events.append(event)

    async def aclose(self) -> None:
        return None


class NullExecutionEventSink:
    """No-op sink used as the emitter default until K3/K4 wire real sinks."""

    async def emit(self, event: CoreExecutionEvent) -> None:
        return None

    async def aclose(self) -> None:
        return None


class ExecutionEventEmitter:
    """Per-RunContext typed event emitter.

    Chosen delivery model: one bounded ``asyncio.Queue`` plus one worker
    task per emitter instance.  ``emit`` stays synchronous (allocate event,
    snapshot trace context, enqueue); the worker awaits every sink with
    per-sink exception isolation.  A full queue drops the event with a
    warning — the same degradation semantics as the legacy
    async dispatcher.
    """

    _registry: dict[RunContext, "ExecutionEventEmitter"] = {}
    _registry_lock = threading.Lock()

    def __init__(
        self,
        run_context: RunContext,
        sinks: Iterable[ExecutionEventSink] = (),
        *,
        queue_size: int = 500,
    ) -> None:
        self._run_context = run_context
        self._sinks: list[ExecutionEventSink] = list(sinks) or [NullExecutionEventSink()]
        self._queue: asyncio.Queue[CoreExecutionEvent | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._seq = 0
        self._closed = False

    @classmethod
    def for_context(
        cls,
        run_context: RunContext,
        sinks: Iterable[ExecutionEventSink] | None = None,
    ) -> ExecutionEventEmitter:
        """Return the shared emitter registered for ``run_context``.

        ``sinks`` is only honoured when the emitter is first created; later
        calls return the existing emitter unchanged.
        """
        with cls._registry_lock:
            emitter = cls._registry.get(run_context)
            if emitter is None or emitter._closed:
                emitter = cls(
                    run_context,
                    sinks=sinks if sinks is not None else (),
                )
                cls._registry[run_context] = emitter
            return emitter

    @classmethod
    def current(cls) -> ExecutionEventEmitter:
        """Return the emitter for the RunContext installed on this task.

        Fail closed when no RunContext is active: typed events must never be
        emitted without their durable run identity.
        """
        run_context = current_run_context.get()
        if run_context is None:
            raise RunContextUnavailableError(
                "ExecutionEventEmitter.current() called without a RunContext; "
                "wrap the call in set_run_context(...)"
            )
        return cls.for_context(run_context)

    def attach_sink(self, sink: ExecutionEventSink) -> None:
        """Register an additional sink; duplicate object references are ignored."""
        if any(existing is sink for existing in self._sinks):
            return
        self._sinks.append(sink)

    def emit(
        self,
        type: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> CoreExecutionEvent:
        """Allocate and enqueue one typed event; returns the event.

        ``type`` is validated by ``CoreExecutionEvent`` (unknown types raise
        ``pydantic.ValidationError``).  The OTel trace context is snapshotted
        synchronously here, inside the caller task.
        """
        if self._closed:
            raise RuntimeError("ExecutionEventEmitter is closed")
        trace_ctx = current_trace_context()
        json_safe_data = safe_serialize(data) if data is not None else {}
        event = CoreExecutionEvent(
            event_id=uuid.uuid4(),
            run_id=self._run_context.run_id,
            attempt=self._run_context.attempt,
            seq=self._next_seq(),
            type=type,
            occurred_at=datetime.now(UTC),
            workspace_id=self._run_context.workspace_id,
            request_id=self._run_context.request_id,
            trace_id=trace_ctx.get("trace_id"),
            span_id=trace_ctx.get("span_id"),
            data=json_safe_data,
        )
        self._enqueue(event)
        return event

    async def drain(self) -> None:
        """Wait until every event enqueued so far has been delivered."""
        if self._closed:
            return
        self._ensure_worker()
        if self._worker_task is not None:
            await self._queue.join()

    async def close(self) -> None:
        """Drain, stop the worker and close every sink."""
        if self._closed:
            return
        await self.drain()
        if self._worker_task is not None and not self._worker_task.done():
            await self._queue.put(None)
            await self._worker_task
        self._worker_task = None
        for sink in self._sinks:
            try:
                await sink.aclose()
            except Exception as exc:
                logger.error(
                    f"[ExecutionEventEmitter] sink close failed: {sink!r}: {exc}"
                )
        self._closed = True
        with self._registry_lock:
            if self._registry.get(self._run_context) is self:
                del self._registry[self._run_context]

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _enqueue(self, event: CoreExecutionEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "[ExecutionEventEmitter] queue full "
                f"(maxsize={self._queue.maxsize}), dropping event "
                f"{event.type} seq={event.seq}"
            )
            return
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: the event stays queued until the next
            # drain()/close() call inside a loop starts the worker.
            return
        self._worker_task = loop.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            try:
                for sink in self._sinks:
                    try:
                        await sink.emit(event)
                    except Exception as exc:
                        logger.error(
                            f"[ExecutionEventEmitter] sink {sink!r} failed "
                            f"for {event.type} seq={event.seq}: {exc}"
                        )
            finally:
                self._queue.task_done()
