"""K1 unit tests: CoreExecutionEvent schema and ExecutionEventEmitter.

Locks the construction contract (JSON-only data, 64KiB inline limit,
seq >= 1, stream.terminal data shape) and the emitter skeleton behavior
(fail-closed RunContext, per-run monotonic seq, trace snapshot at emit,
sink exception isolation, drain/close).
"""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from map_core.service.execution_event import (
    INLINE_PAYLOAD_MAX_BYTES,
    CoreExecutionEvent,
    ExecutionEventEmitter,
    InMemoryExecutionEventSink,
    RunContext,
    RunContextUnavailableError,
    current_run_context,
    set_run_context,
)


def _run_id() -> uuid.UUID:
    return uuid.uuid4()


def _base_event(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "event_id": uuid.uuid4(),
        "run_id": _run_id(),
        "attempt": 1,
        "seq": 1,
        "type": "step.started",
        "occurred_at": datetime.now(UTC),
        "data": {},
    }
    fields.update(overrides)
    return fields


class _RaisingSink:
    def __init__(self) -> None:
        self.emitted = 0
        self.closed = False

    async def emit(self, event: CoreExecutionEvent) -> None:
        self.emitted += 1
        raise RuntimeError("sink exploded")

    async def aclose(self) -> None:
        self.closed = True


class _TrackingSink:
    def __init__(self) -> None:
        self.events: list[CoreExecutionEvent] = []
        self.closed = False

    async def emit(self, event: CoreExecutionEvent) -> None:
        self.events.append(event)

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------- schema


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CoreExecutionEvent(**_base_event(type="run.started"))


def test_data_must_be_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CoreExecutionEvent(**_base_event(data={"score": math.nan}))
    assert "non-finite float" in str(exc_info.value)


def test_data_must_be_canonical_json_rejects_non_string_keys() -> None:
    with pytest.raises(ValidationError):
        CoreExecutionEvent(**_base_event(data={1: "value"}))


def test_data_must_be_canonical_json_rejects_bytes() -> None:
    with pytest.raises(ValidationError):
        CoreExecutionEvent(**_base_event(data={"blob": b"bytes"}))


def test_data_inline_limit_rejects_over_64kib() -> None:
    too_big = {"text": "a" * (INLINE_PAYLOAD_MAX_BYTES + 1)}
    with pytest.raises(ValidationError) as exc_info:
        CoreExecutionEvent(**_base_event(data=too_big))
    assert "inline limit" in str(exc_info.value)


def test_data_inline_limit_accepts_exactly_64kib_payload() -> None:
    # {"text":"..."} is len(text) + 11 UTF-8 bytes; target exactly 64KiB.
    text = "a" * (INLINE_PAYLOAD_MAX_BYTES - 11)
    event = CoreExecutionEvent(**_base_event(data={"text": text}))
    assert len(event.data["text"]) == INLINE_PAYLOAD_MAX_BYTES - 11


def test_seq_must_be_positive_integer() -> None:
    with pytest.raises(ValidationError):
        CoreExecutionEvent(**_base_event(seq=0))
    with pytest.raises(ValidationError):
        CoreExecutionEvent(**_base_event(seq=True))


def test_stream_terminal_requires_status_error_code_error_message() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CoreExecutionEvent(**_base_event(type="stream.terminal", data={"status": "completed"}))
    assert "stream.terminal" in str(exc_info.value)

    event = CoreExecutionEvent(
        **_base_event(
            type="stream.terminal",
            data={
                "status": "completed",
                "error_code": None,
                "error_message": None,
            },
        )
    )
    assert event.type == "stream.terminal"
    assert event.data["status"] == "completed"
    assert event.data["error_code"] is None
    assert event.data["error_message"] is None


# ---------------------------------------------------------------- RunContext


def test_set_run_context_installs_and_restores_contextvar() -> None:
    assert current_run_context.get() is None
    run_id = _run_id()
    with set_run_context(run_id=run_id, attempt=2, request_id="req-1"):
        ctx = current_run_context.get()
        assert ctx is not None
        assert ctx.run_id == run_id
        assert ctx.attempt == 2
        assert ctx.request_id == "req-1"
    assert current_run_context.get() is None


def test_emitter_current_fails_closed_without_run_context() -> None:
    with pytest.raises(RunContextUnavailableError):
        ExecutionEventEmitter.current()


# ---------------------------------------------------------------- emitter


def test_emit_allocates_per_run_monotonic_seq_and_identity() -> None:
    run_id = _run_id()
    workspace_id = uuid.uuid4()
    sink = InMemoryExecutionEventSink()
    ctx = RunContext(
        run_id=run_id,
        workspace_id=workspace_id,
        attempt=3,
        request_id="req-42",
        session_id="session-42",
        staff_code="staff-42",
    )
    emitter = ExecutionEventEmitter(ctx, sinks=[sink])

    first = emitter.emit("step.started", data={"n": 1})
    second = emitter.emit("step.completed", data={"n": 2})

    assert first.seq == 1
    assert second.seq == 2
    assert first.run_id == run_id
    assert first.workspace_id == workspace_id
    assert first.attempt == 3
    assert first.request_id == "req-42"
    assert first.occurred_at.tzinfo is not None
    assert first.data == {"n": 1}

    async def scenario() -> None:
        await emitter.close()

    asyncio.run(scenario())
    assert [e.seq for e in sink.events] == [1, 2]
    assert [e.type for e in sink.events] == ["step.started", "step.completed"]


def test_emit_snapshots_trace_context_before_enqueue() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    sink = InMemoryExecutionEventSink()
    emitter = ExecutionEventEmitter(
        RunContext(run_id=_run_id()), sinks=[sink]
    )

    expected_trace_id: str | None = None
    expected_span_id: str | None = None

    async def scenario() -> None:
        nonlocal expected_trace_id, expected_span_id
        with tracer.start_as_current_span("request.span") as span:
            span_ctx = span.get_span_context()
            expected_trace_id = format(span_ctx.trace_id, "032x")
            expected_span_id = format(span_ctx.span_id, "016x")
            emitter.emit("step.started", data={"inside": "span"})
        # span is closed here; drain must still deliver the snapshot.
        await emitter.drain()

    asyncio.run(scenario())
    assert len(sink.events) == 1
    assert sink.events[0].trace_id == expected_trace_id
    assert sink.events[0].span_id == expected_span_id


def test_sink_exception_does_not_break_emit_or_drain() -> None:
    raising = _RaisingSink()
    tracking = _TrackingSink()
    emitter = ExecutionEventEmitter(RunContext(run_id=_run_id()), sinks=[raising, tracking])

    event = emitter.emit("step.started")
    assert event.seq == 1

    async def scenario() -> None:
        await emitter.drain()

    asyncio.run(scenario())
    # the failing sink still saw the event; the healthy sink also saw it
    assert raising.emitted == 1
    assert [e.seq for e in tracking.events] == [1]


def test_drain_and_close_deliver_then_close_sinks() -> None:
    sink = InMemoryExecutionEventSink()
    emitter = ExecutionEventEmitter(RunContext(run_id=_run_id()), sinks=[sink])
    emitter.emit("checkpoint.written", data={"name": "ckpt"})
    emitter.emit("effect.planned")

    async def scenario() -> None:
        await emitter.drain()
        assert len(sink.events) == 2
        await emitter.close()

    asyncio.run(scenario())
    assert len(sink.events) == 2
    assert sink.events[0].data == {"name": "ckpt"}


def test_current_returns_registry_singleton_per_run_context() -> None:
    run_id = _run_id()
    with set_run_context(run_id=run_id):
        emitter = ExecutionEventEmitter.current()
        assert isinstance(emitter, ExecutionEventEmitter)
        assert ExecutionEventEmitter.current() is emitter

    # the contextvar resets; a new run id gets a new emitter
    with set_run_context(run_id=_run_id()):
        assert ExecutionEventEmitter.current() is not emitter


def test_otel_projector_adds_span_event_with_redacted_attributes() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    sink = InMemoryExecutionEventSink()
    emitter = ExecutionEventEmitter(RunContext(run_id=_run_id()), sinks=[sink])

    async def scenario() -> None:
        with tracer.start_as_current_span("request.span"):
            emitter.emit(
                "step.completed",
                data={
                    "component": "scene_selector",
                    "status": "success",
                    "secret": "must-not-leak",
                },
            )
        await emitter.drain()

    asyncio.run(scenario())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    events = spans[0].events
    assert [event.name for event in events] == ["map.execution_event"]
    attrs = dict(events[0].attributes or {})
    assert attrs["type"] == "step.completed"
    assert attrs["component"] == "scene_selector"
    assert attrs["status"] == "success"
    assert "data" not in attrs
    assert "secret" not in attrs


def test_otel_projector_is_noop_without_span() -> None:
    sink = InMemoryExecutionEventSink()
    emitter = ExecutionEventEmitter(RunContext(run_id=_run_id()), sinks=[sink])

    event = emitter.emit("step.started", data={"component": "flow"})
    assert event.seq == 1

    async def scenario() -> None:
        await emitter.drain()
        await emitter.close()

    # Must not raise and the event still reaches the regular sink.
    asyncio.run(scenario())
    assert [e.type for e in sink.events] == ["step.started"]


def test_queue_full_drops_event_without_raising() -> None:
    sink = _TrackingSink()
    emitter = ExecutionEventEmitter(
        RunContext(run_id=_run_id()), sinks=[sink], queue_size=1
    )

    async def scenario() -> None:
        # no worker started yet: the first emit fills the single slot, the
        # second one must be dropped synchronously without an exception.
        first = emitter.emit("step.started")
        second = emitter.emit("step.completed")
        assert first.seq == 1
        assert second.seq == 2
        await emitter.drain()
        await emitter.close()

    asyncio.run(scenario())
    assert [e.seq for e in sink.events] == [1]
