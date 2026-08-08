"""Re-review (round 3) P2-5.4 acceptance tests: configure_bff_telemetry.

The earlier BFF span tests only covered instrument_app() with an in-memory
provider; the production configuration path (switches, protocol, sampling,
idempotency, shutdown) had no direct coverage. Mirrors the semantics of
map_core's tests/test_telemetry_switch.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpOTLPSpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app import telemetry as telemetry_module
from otel_env import OTEL_ENV_VARS as _OTEL_ENV_VARS

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_telemetry(monkeypatch):
    for var in _OTEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(telemetry_module, "_provider", None)
    monkeypatch.setattr(telemetry_module, "_shut_down", False)
    # keep instrumentation and the global provider out of these tests;
    # test_bff_spans.py covers the instrumentation path separately and
    # test_shutdown_is_terminal exercises the REAL provider lifecycle
    monkeypatch.setattr(telemetry_module, "instrument_app", lambda app, provider: None)
    from opentelemetry import trace

    monkeypatch.setattr(trace, "set_tracer_provider", lambda *_: None)
    yield
    provider = telemetry_module._provider
    if provider is not None:
        with contextlib.suppress(Exception):
            provider.shutdown()
        telemetry_module._provider = None
        telemetry_module._shut_down = False


def _configure() -> bool:
    return telemetry_module.configure_bff_telemetry(app=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("env", "expected_enabled"),
    [
        # stray endpoint must NOT enable OTel (same semantics as map_core)
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, False),
        ({}, False),
        (
            {
                "MAP_OTEL_ENABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            },
            True,
        ),
        ({"MAP_OTEL_ENABLED": "true"}, True),
        (
            {
                "MAP_OTEL_ENABLED": "false",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            },
            False,
        ),
        (
            {
                "MAP_OTEL_ENABLED": "true",
                "OTEL_SDK_DISABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            },
            False,
        ),
    ],
)
def test_configure_switch_combinations(
    monkeypatch, clean_telemetry, env, expected_enabled
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert _configure() is expected_enabled
    assert (telemetry_module._provider is not None) is expected_enabled


@pytest.mark.parametrize(
    ("env", "expected_type"),
    [
        ({}, HttpOTLPSpanExporter),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"}, HttpOTLPSpanExporter),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, GrpcOTLPSpanExporter),
        # per-signal protocol wins over the common one (map_core semantics)
        (
            {
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc",
            },
            GrpcOTLPSpanExporter,
        ),
    ],
)
def test_exporter_protocol_selection(
    monkeypatch, env, expected_type
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    exporter = telemetry_module._build_span_exporter()
    assert isinstance(exporter, expected_type)


def test_exporter_protocol_rejects_unknown(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "carrier-pigeon")
    with pytest.raises(ValueError, match="unsupported OTLP trace protocol"):
        telemetry_module._build_span_exporter()


@pytest.mark.parametrize(
    ("raw", "expected_rate"),
    [
        ("0.25", 0.25),
        ("-3", 0.0),  # clamped low
        ("7", 1.0),  # clamped high
        ("not-a-number", 1.0),  # invalid falls back to full sampling
    ],
)
def test_sampling_ratio_bounds_and_invalid(
    monkeypatch, clean_telemetry, raw, expected_rate
) -> None:
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_SAMPLING_RATIO", raw)
    assert _configure() is True
    sampler = telemetry_module._provider.sampler
    assert isinstance(sampler, ParentBased)
    assert isinstance(sampler._root, TraceIdRatioBased)
    assert sampler._root._rate == expected_rate


def test_parent_based_honors_inbound_sampling_flag(
    monkeypatch, clean_telemetry
) -> None:
    """A ratio of 0 must still record traces the upstream marked sampled."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import SpanContext, TraceFlags

    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_SAMPLING_RATIO", "0")
    assert _configure() is True
    sampler = telemetry_module._provider.sampler

    parent_ctx = SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    from opentelemetry.trace import set_span_in_context

    sampled = sampler.should_sample(
        set_span_in_context(otel_trace.NonRecordingSpan(parent_ctx)),
        trace_id=parent_ctx.trace_id,
        name="inbound",
    )
    assert sampled.decision.is_sampled()

    unsampled_parent = SpanContext(
        trace_id=parent_ctx.trace_id,
        span_id=parent_ctx.span_id,
        is_remote=True,
        trace_flags=TraceFlags(0),
    )
    dropped = sampler.should_sample(
        set_span_in_context(otel_trace.NonRecordingSpan(unsampled_parent)),
        trace_id=unsampled_parent.trace_id,
        name="inbound",
    )
    assert not dropped.decision.is_sampled()


def test_configure_is_idempotent(monkeypatch, clean_telemetry) -> None:
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    assert _configure() is True
    first = telemetry_module._provider
    assert _configure() is True
    assert telemetry_module._provider is first


def test_exporter_tuning_values_reach_processor(
    monkeypatch, clean_telemetry
) -> None:
    """Round 6 P2-4.2: non-default tuning values are consumed by the SDK.

    The same input set as the compose tuning test (queue=64, batch=32) must
    initialize a BatchSpanProcessor successfully and the values must land on
    the processor — proving the Compose pass-through is not just string
    copying. The export timeout is deliberately NOT asserted here: the SDK
    stores export_timeout_millis but does not wire it into exports (SDK
    1.44); test_exporter_timeout_reaches_exporter covers the real path.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setattr(
        telemetry_module,
        "_build_span_exporter",
        lambda: InMemorySpanExporter(),
    )
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_MAX_QUEUE_SIZE", "64")
    monkeypatch.setenv("MAP_OTEL_MAX_EXPORT_BATCH_SIZE", "32")

    assert _configure() is True
    inner = (
        telemetry_module._provider._active_span_processor._span_processors[0]
        ._batch_processor
    )
    assert inner._max_queue_size == 64
    assert inner._max_export_batch_size == 32


def test_exporter_timeout_reaches_exporter(monkeypatch, clean_telemetry) -> None:
    """Round 7 P2-4.1: MAP_OTEL_EXPORT_TIMEOUT_MS lands on the exporter.

    The SDK stores BatchSpanProcessor.export_timeout_millis but does not use
    it (SDK 1.44: "Not used. No way currently to pass timeout to export.").
    The OTLP exporter ``timeout`` (seconds) is the only real per-request
    limit, so this asserts on the exporter constructor argument for both the
    gRPC and the HTTP branch, including ms -> s conversion and the fallback
    for non-numeric input.
    """
    from opentelemetry.exporter.otlp.proto.grpc import (
        trace_exporter as grpc_trace_exporter,
    )
    from opentelemetry.exporter.otlp.proto.http import (
        trace_exporter as http_trace_exporter,
    )
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    captured: list[dict] = []

    class _RecordingExporter(InMemorySpanExporter):
        def __init__(self, **kwargs):
            captured.append(kwargs)
            super().__init__()

    monkeypatch.setattr(
        grpc_trace_exporter, "OTLPSpanExporter", _RecordingExporter
    )
    monkeypatch.setattr(
        http_trace_exporter, "OTLPSpanExporter", _RecordingExporter
    )

    monkeypatch.setenv("MAP_OTEL_EXPORT_TIMEOUT_MS", "1234")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    telemetry_module._build_span_exporter()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    telemetry_module._build_span_exporter()
    assert captured == [{"timeout": 1.234}, {"timeout": 1.234}]

    # the FULL configure path must survive a bad value as well: the exporter
    # falls back to the default 10s and the processor gets the same parsed
    # value instead of crashing startup with a ValueError
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_MAX_QUEUE_SIZE", "64")
    monkeypatch.setenv("MAP_OTEL_MAX_EXPORT_BATCH_SIZE", "32")
    monkeypatch.setenv("MAP_OTEL_EXPORT_TIMEOUT_MS", "not-a-number")
    assert _configure() is True
    assert captured[-1] == {"timeout": 10.0}


def test_exporter_tuning_rejects_batch_larger_than_queue(
    monkeypatch, clean_telemetry
) -> None:
    """Round 6 P2-4.2: SDK constraint max_export_batch_size <= max_queue_size.

    A violating pair must fail fast at startup with the SDK's diagnostic
    error instead of silently degrading, and must not leave a half-built
    provider behind.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setattr(
        telemetry_module,
        "_build_span_exporter",
        lambda: InMemorySpanExporter(),
    )
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_MAX_QUEUE_SIZE", "32")
    monkeypatch.setenv("MAP_OTEL_MAX_EXPORT_BATCH_SIZE", "64")

    with pytest.raises(ValueError, match="max_export_batch_size"):
        _configure()
    assert telemetry_module._provider is None


# The one-shot lifecycle (real global TracerProvider + real FastAPI/httpx
# instrumentation) cannot be installed/uninstalled inside the pytest process:
# the global provider is set once and the httpx instrumentation cannot be
# undone. Round 5 P2-4.2 therefore runs the probe in a SEPARATE subprocess
# and only asserts on its exit code and structured output.
_TERMINAL_LIFECYCLE_PROBE = """
import json
import os
import sys

sys.path.insert(0, os.getcwd())

# Hermetic env for the probe: enable OTel, drop anything the host may leak.
os.environ["MAP_OTEL_ENABLED"] = "true"
os.environ.pop("OTEL_SDK_DISABLED", None)
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
os.environ.pop("OTEL_EXPORTER_OTLP_PROTOCOL", None)

from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app import telemetry

# Export target only; the provider + instrumentation lifecycle stays real.
telemetry._build_span_exporter = lambda: InMemorySpanExporter()

app = FastAPI()
first = telemetry.configure_bff_telemetry(app)
provider = telemetry._provider

telemetry.shutdown_bff_telemetry()
flush_noop = True
try:
    telemetry.flush_bff_telemetry()
except Exception:
    flush_noop = False
after = telemetry.configure_bff_telemetry(app)

result = {
    "first_configure": first,
    "provider_preserved": telemetry._provider is provider,
    "shut_down": telemetry._shut_down,
    "flush_after_shutdown_noop": flush_noop,
    "reconfigure_after_shutdown": after,
}
print(json.dumps(result))

ok = (
    first is True
    and provider is not None
    and telemetry._provider is provider
    and telemetry._shut_down is True
    and flush_noop is True
    and after is False
)
sys.exit(0 if ok else 1)
"""


def test_shutdown_is_terminal() -> None:
    """Round 4 P2-4.2: shutdown is a process-lifetime terminal state.

    Round 5 P2-4.2: executed in an isolated subprocess. The real global
    TracerProvider and the real FastAPI/httpx instrumentation (which cannot
    be reinstalled in-process) are installed and torn down inside the child,
    so the pytest process keeps its provider/instrumentation state untouched
    regardless of test order. The parent asserts the child's exit code, its
    structured JSON output, and that no provider-override or duplicate-
    instrumentation warning was emitted.
    """
    env = {k: v for k, v in os.environ.items() if k not in _OTEL_ENV_VARS}
    result = subprocess.run(
        [sys.executable, "-c", _TERMINAL_LIFECYCLE_PROBE],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        "terminal lifecycle probe failed\\n"
        f"stdout={result.stdout}\\nstderr={result.stderr}"
    )
    # even inside the isolated child the real SDK must not warn about
    # provider overriding or duplicate instrumentation
    assert "Overriding of current TracerProvider" not in result.stderr
    assert "Attempting to instrument" not in result.stderr

    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["first_configure"] is True
    assert state["provider_preserved"] is True
    assert state["shut_down"] is True
    assert state["flush_after_shutdown_noop"] is True
    assert state["reconfigure_after_shutdown"] is False
