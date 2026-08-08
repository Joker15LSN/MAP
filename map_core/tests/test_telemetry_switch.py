"""P1 acceptance tests: OTel enablement switch semantics.

Regression for the review finding that a stray OTEL_EXPORTER_OTLP_ENDPOINT
implicitly enabled exporters even when MAP_OTEL_ENABLED=false.

Priority: OTEL_SDK_DISABLED=true > MAP_OTEL_ENABLED=false > enabled.
Endpoints only configure the export target, never enablement.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger

from map_core.observability import telemetry as telemetry_module

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Every env var configure_telemetry() / the ASGI middleware reads — keep in
# sync with map_core/observability/telemetry.py and asgi.py. The same list
# drives the clean_telemetry fixture and the subprocess env filtering, so the
# pytest process and the lifecycle probes share one isolation contract.
_OTEL_ENV_VARS = (
    # enablement / kill switch
    "MAP_OTEL_ENABLED",
    "OTEL_SDK_DISABLED",
    # endpoints (common + per-signal)
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    # protocols (common + per-signal)
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    # sampling / identity
    "MAP_OTEL_SAMPLING_RATIO",
    "OTEL_SERVICE_NAME",
    "MAP_SERVICE_VERSION",
    "PHOENIX_PROJECT_NAME",
    # trace exporter tuning
    "MAP_OTEL_MAX_QUEUE_SIZE",
    "MAP_OTEL_MAX_EXPORT_BATCH_SIZE",
    "MAP_OTEL_EXPORT_TIMEOUT_MS",
    # native-log pipeline
    "MAP_OTEL_NATIVE_LOG_EXPORT_ENABLED",
    "MAP_OTEL_LOG_MAX_QUEUE_SIZE",
    "MAP_OTEL_LOG_MAX_EXPORT_BATCH_SIZE",
    "MAP_OTEL_LOG_EXPORT_TIMEOUT_MS",
    "MAP_OTEL_LOGS_AS_SPAN_EVENTS",
    "MAP_OTEL_LOG_LEVEL",
    "MAP_OTEL_LOG_MESSAGE_MAX_CHARS",
    # instrumentation exclusions (ASGI middleware)
    "MAP_OTEL_EXCLUDED_PATHS",
)


@pytest.fixture
def clean_telemetry(monkeypatch):
    for var in _OTEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(telemetry_module, "_provider", None)
    monkeypatch.setattr(telemetry_module, "_logger_provider", None)
    monkeypatch.setattr(telemetry_module, "_logging_handler", None)
    monkeypatch.setattr(telemetry_module, "_loguru_sink_id", None)
    monkeypatch.setattr(telemetry_module, "_telemetry_tracer", None)
    monkeypatch.setattr(telemetry_module, "_shut_down", False)
    # avoid touching the real global tracer provider
    monkeypatch.setattr(telemetry_module.trace, "set_tracer_provider", lambda *_: None)
    yield
    # stop any background log exporter started by the enabled path so no
    # retry noise leaks into other tests
    if telemetry_module._logger_provider is not None:
        try:
            telemetry_module._logger_provider.shutdown()
        except Exception:
            pass
    # stop the span batch worker + exporter thread started by the enabled
    # path (mirrors the BFF fixture; otherwise a daemon thread per enabling
    # test keeps retrying against a dead collector)
    if telemetry_module._provider is not None:
        try:
            telemetry_module._provider.shutdown()
        except Exception:
            pass
    # remove any loguru sink registered by the enabled path
    sink_id = telemetry_module._loguru_sink_id
    if sink_id is not None:
        try:
            logger.remove(sink_id)
        except ValueError:
            pass


def _configure() -> bool:
    return telemetry_module.configure_telemetry(
        service_name="map-core",
        service_version="0.0.0-test",
        deployment_environment="test",
    )


@pytest.mark.parametrize(
    ("env", "expected_enabled"),
    [
        # stray endpoint must NOT enable OTel (core regression)
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, False),
        # nothing configured -> disabled
        ({}, False),
        # explicit enable with endpoint -> enabled
        (
            {
                "MAP_OTEL_ENABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            },
            True,
        ),
        # explicit enable without endpoint still enabled (SDK defaults apply)
        ({"MAP_OTEL_ENABLED": "true"}, True),
        # MAP_OTEL_ENABLED=false wins over endpoint
        (
            {
                "MAP_OTEL_ENABLED": "false",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            },
            False,
        ),
        # OTEL_SDK_DISABLED has the highest priority
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
def test_configure_telemetry_switch_combinations(
    monkeypatch, clean_telemetry, env, expected_enabled
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result = _configure()

    assert result is expected_enabled
    assert (telemetry_module._provider is not None) is expected_enabled


def test_configure_telemetry_is_idempotent(monkeypatch, clean_telemetry) -> None:
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    assert _configure() is True
    # second call short-circuits without rebuilding providers
    assert _configure() is True


def test_exporter_tuning_values_reach_processor(
    monkeypatch, clean_telemetry
) -> None:
    """Round 6 P2-4.2: non-default tuning values are consumed by the SDK.

    The same input set as the compose tuning test (queue=64, batch=32) must
    initialize a BatchSpanProcessor successfully and the values must land on
    the processor. The export timeout is deliberately NOT asserted here: the
    SDK stores export_timeout_millis but does not wire it into exports (SDK
    1.44); test_export_timeout_reaches_exporter covers the real path.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setattr(
        telemetry_module,
        "HttpOTLPSpanExporter",
        lambda *args, **kwargs: InMemorySpanExporter(),
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


def test_export_timeout_reaches_exporter(monkeypatch, clean_telemetry) -> None:
    """Round 7 P2-4.1: MAP_OTEL_*_EXPORT_TIMEOUT_MS land on the exporters.

    The SDK stores BatchSpanProcessor/BatchLogRecordProcessor
    ``export_timeout_millis`` but does not use it (SDK 1.44: "Not used. No
    way currently to pass timeout to export."). The OTLP exporter ``timeout``
    (seconds) is the only real per-request limit, so this asserts on the
    exporter constructor arguments for ALL four branches — trace grpc/http
    and native-log grpc/http — including ms -> s conversion.
    """
    trace_captured: list[dict] = []
    log_captured: list[dict] = []

    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    class _RecordingSpanExporter(InMemorySpanExporter):
        def __init__(self, **kwargs):
            trace_captured.append(kwargs)
            super().__init__()

    class _RecordingLogExporter(InMemoryLogRecordExporter):
        def __init__(self, **kwargs):
            log_captured.append(kwargs)
            super().__init__()

    # map_core binds the exporter classes at module import time, so the
    # recording classes must replace telemetry_module's own attributes
    # (patching the source modules would not affect the bound names).
    monkeypatch.setattr(
        telemetry_module, "GrpcOTLPSpanExporter", _RecordingSpanExporter
    )
    monkeypatch.setattr(
        telemetry_module, "HttpOTLPSpanExporter", _RecordingSpanExporter
    )
    monkeypatch.setattr(
        telemetry_module, "GrpcOTLPLogExporter", _RecordingLogExporter
    )
    monkeypatch.setattr(
        telemetry_module, "HttpOTLPLogExporter", _RecordingLogExporter
    )

    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_EXPORT_TIMEOUT_MS", "1234")
    monkeypatch.setenv("MAP_OTEL_LOG_EXPORT_TIMEOUT_MS", "777")
    monkeypatch.setenv("MAP_OTEL_LOGS_AS_SPAN_EVENTS", "false")

    def _reset_state() -> None:
        monkeypatch.setattr(telemetry_module, "_provider", None)
        monkeypatch.setattr(telemetry_module, "_logger_provider", None)
        monkeypatch.setattr(telemetry_module, "_logging_handler", None)
        monkeypatch.setattr(telemetry_module, "_loguru_sink_id", None)
        monkeypatch.setattr(telemetry_module, "_telemetry_tracer", None)
        monkeypatch.setattr(telemetry_module, "_shut_down", False)

    # grpc branch: the common protocol drives both pipelines
    _reset_state()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://logs:4317")
    assert _configure() is True
    # http branch
    _reset_state()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    assert _configure() is True

    assert trace_captured == [{"timeout": 1.234}, {"timeout": 1.234}]
    assert log_captured == [{"timeout": 0.777}, {"timeout": 0.777}]

    # the FULL configure path must survive bad values: exporters fall back
    # to the default 10s and the processors get the same parsed value
    # instead of crashing startup with a ValueError
    _reset_state()
    monkeypatch.setenv("MAP_OTEL_EXPORT_TIMEOUT_MS", "not-a-number")
    monkeypatch.setenv("MAP_OTEL_LOG_EXPORT_TIMEOUT_MS", "nope")
    assert _configure() is True
    assert trace_captured[-1] == {"timeout": 10.0}
    assert log_captured[-1] == {"timeout": 10.0}


def test_exporter_tuning_rejects_batch_larger_than_queue(
    monkeypatch, clean_telemetry
) -> None:
    """Round 6 P2-4.2: SDK constraint max_export_batch_size <= max_queue_size.

    A violating pair must fail fast at startup and must not leave a
    half-built provider behind.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setattr(
        telemetry_module,
        "HttpOTLPSpanExporter",
        lambda *args, **kwargs: InMemorySpanExporter(),
    )
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_MAX_QUEUE_SIZE", "32")
    monkeypatch.setenv("MAP_OTEL_MAX_EXPORT_BATCH_SIZE", "64")

    with pytest.raises(ValueError, match="max_export_batch_size"):
        _configure()
    assert telemetry_module._provider is None


# The one-shot lifecycle (real global TracerProvider/LoggerProvider) cannot
# be installed/uninstalled inside the pytest process: the global provider is
# set once. Round 5 P2-4.2 therefore runs the probe in a SEPARATE subprocess
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
os.environ.pop("MAP_OTEL_NATIVE_LOG_EXPORT_ENABLED", None)
os.environ.pop("MAP_OTEL_LOGS_AS_SPAN_EVENTS", None)

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from map_core.observability import telemetry

# Export target only; the provider lifecycle under test stays real.
telemetry.HttpOTLPSpanExporter = lambda *args, **kwargs: InMemorySpanExporter()

first = telemetry.configure_telemetry(
    service_name="map-core",
    service_version="0.0.0-test",
    deployment_environment="test",
)
provider = telemetry._provider

telemetry.shutdown_telemetry()
after = telemetry.configure_telemetry(
    service_name="map-core",
    service_version="0.0.0-test",
    deployment_environment="test",
)

result = {
    "first_configure": first,
    "provider_preserved": telemetry._provider is provider,
    "shut_down": telemetry._shut_down,
    "reconfigure_after_shutdown": after,
}
print(json.dumps(result))

ok = (
    first is True
    and provider is not None
    and telemetry._provider is provider
    and telemetry._shut_down is True
    and after is False
)
sys.exit(0 if ok else 1)
"""


def test_shutdown_is_terminal() -> None:
    """Round 4 P2-4.2: shutdown is a process-lifetime terminal state.

    Round 5 P2-4.2: executed in an isolated subprocess. The real global
    TracerProvider/LoggerProvider (which cannot be reinstalled in-process)
    are installed and torn down inside the child, so the pytest process keeps
    its provider state untouched regardless of test order. The parent asserts
    the child's exit code, its structured JSON output, and that no
    provider-override warning was emitted.
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
    # provider overriding
    assert "Overriding of current TracerProvider" not in result.stderr

    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["first_configure"] is True
    assert state["provider_preserved"] is True
    assert state["shut_down"] is True
    assert state["reconfigure_after_shutdown"] is False


def test_native_log_export_defaults_on_with_logs_endpoint(
    monkeypatch, clean_telemetry
) -> None:
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://collector:4318"
    )
    assert _configure() is True
    assert telemetry_module._logger_provider is not None


def test_native_log_export_force_disabled_over_endpoint(
    monkeypatch, clean_telemetry
) -> None:
    """Re-review P2-5.7: an explicit false switch must beat the endpoint.

    The bundled Collector only runs a traces pipeline, so deployments point a
    logs endpoint at it only by accident; ``MAP_OTEL_NATIVE_LOG_EXPORT_ENABLED=false``
    must win and keep logs on the span-events path.
    """
    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://collector:4318"
    )
    monkeypatch.setenv("MAP_OTEL_NATIVE_LOG_EXPORT_ENABLED", "false")
    assert _configure() is True
    assert telemetry_module._logger_provider is None


def test_sampler_honors_inbound_sampling_decision(
    monkeypatch, clean_telemetry
) -> None:
    """Re-review (round 3) P2-5.3: cross-service sampling must be ParentBased.

    With a local ratio of 0, an inbound traceparent flagged sampled must
    still be recorded (and an unsampled one dropped), otherwise traces break
    whenever services configure different ratios.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.sampling import ParentBased
    from opentelemetry.trace import SpanContext, TraceFlags, set_span_in_context

    monkeypatch.setenv("MAP_OTEL_ENABLED", "true")
    monkeypatch.setenv("MAP_OTEL_SAMPLING_RATIO", "0")
    assert _configure() is True
    sampler = telemetry_module._provider.sampler
    assert isinstance(sampler, ParentBased)

    trace_id = 0x4BF92F3577B34DA6A3CE929D0E0E4736
    span_id = 0x00F067AA0BA902B7
    sampled_parent = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    decision = sampler.should_sample(
        set_span_in_context(otel_trace.NonRecordingSpan(sampled_parent)),
        trace_id=trace_id,
        name="inbound",
    )
    assert decision.decision.is_sampled()

    unsampled_parent = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(0),
    )
    decision = sampler.should_sample(
        set_span_in_context(otel_trace.NonRecordingSpan(unsampled_parent)),
        trace_id=trace_id,
        name="inbound",
    )
    assert not decision.decision.is_sampled()
