"""OpenTelemetry setup for the MAP BFF.

Goal: every regular request forms a continuous trace

    BFF SERVER -> BFF CLIENT -> map_core SERVER -> LLM / TOOL / MCP

The FastAPI instrumentation creates the SERVER span from the inbound W3C
context (or mints a root span when absent); the httpx instrumentation
creates CLIENT spans around MapCoreClient calls and injects a dynamically
generated traceparent, so map_core joins the same trace even when the
browser sent no propagation headers.

Enablement semantics mirror map_core (priority order):
  1. ``OTEL_SDK_DISABLED=true`` force-disables everything;
  2. ``MAP_OTEL_ENABLED`` must be truthy — the ONLY switch that turns OTel on;
  3. ``OTEL_EXPORTER_OTLP_*`` only configure the export target.

Export protocol mirrors map_core as well: ``OTEL_EXPORTER_OTLP_TRACES_PROTOCOL``
falls back to ``OTEL_EXPORTER_OTLP_PROTOCOL`` and defaults to
``http/protobuf``; ``grpc`` selects the gRPC exporter, so the BFF can share
one collector endpoint/protocol with map_core.

Sampling uses ``ParentBased(root=TraceIdRatioBased(ratio))`` so an inbound
sampled decision is always honored — cross-service traces never break just
because services configure different ratios.
"""

from __future__ import annotations

import math
import os
from typing import Any

from fastapi import FastAPI

_SERVICE_NAME = "map-business-backend"

# Module-level handle so tests/ops can flush or shut the provider down and
# repeated configure calls stay idempotent. ``_shut_down`` marks the
# process-lifetime terminal state: the FastAPI/httpx instrumentation keeps
# its ORIGINAL provider (re-pointing the global TracerProvider would not
# re-attach it), so after shutdown telemetry must NOT be re-enabled in this
# process — a rebuilt provider would silently export nothing.
_provider: Any | None = None
_shut_down = False


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_trace_protocol() -> str:
    return os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )


def _export_timeout_seconds(
    env_key: str = "MAP_OTEL_EXPORT_TIMEOUT_MS", default_ms: int = 10000
) -> float:
    """Milliseconds from env -> seconds for the OTLP exporter ``timeout``.

    The OTLP exporters take ``timeout`` in seconds; the SDK stores
    BatchSpanProcessor.export_timeout_millis but does NOT wire it into
    exports (SDK 1.44: "Not used. No way currently to pass timeout to
    export."), so the exporter timeout is the only real per-request limit.
    Non-numeric, NaN/inf or negative values fall back to the default /
    clamp to 0. Use the same helper for the processor's (inert)
    export_timeout_millis so a bad value can never crash startup.
    """
    try:
        millis = float(os.getenv(env_key, str(default_ms)))
    except (TypeError, ValueError):
        millis = float(default_ms)
    if not math.isfinite(millis):
        millis = float(default_ms)
    return max(millis, 0.0) / 1000.0


def _build_span_exporter() -> Any:
    protocol = _resolve_trace_protocol()
    timeout = _export_timeout_seconds()
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(timeout=timeout)
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(timeout=timeout)
    raise ValueError(
        "unsupported OTLP trace protocol; use 'grpc' or 'http/protobuf'"
    )


def instrument_app(app: FastAPI, tracer_provider) -> None:
    """Attach SERVER/CLIENT instrumentation with an explicit provider.

    Kept separate from :func:`configure_bff_telemetry` so tests can install
    an in-memory provider without touching env vars or OTLP exporters.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        # Same default as map_core and docker-compose.otel.yml so local
        # non-compose runs behave like the fleet.
        excluded_urls=os.getenv("MAP_OTEL_EXCLUDED_PATHS", "/health,/metrics"),
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)


def configure_bff_telemetry(app: FastAPI) -> bool:
    """Enable OTel SDK + instrumentation when the switches allow it.

    Process-lifetime one-shot semantics: once :func:`shutdown_bff_telemetry`
    ran, this returns ``False`` instead of rebuilding the provider. The
    FastAPI/httpx instrumentation is installed exactly once and keeps its
    original provider, so a "reconfigure" after shutdown would be a lie:
    the module state would look configured while the instrumentation still
    exports through the shut-down provider.
    """
    global _provider, _shut_down
    if _shut_down:
        # Terminal state — do NOT resurrect a provider in this process.
        return False
    if _provider is not None:
        # Idempotent: repeated calls (e.g. lifespan restarts in tests) must
        # not stack providers or double-instrument.
        return True

    if _is_truthy(os.getenv("OTEL_SDK_DISABLED")):
        return False
    if not _is_truthy(os.getenv("MAP_OTEL_ENABLED")):
        return False

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    try:
        sampling_ratio = float(os.getenv("MAP_OTEL_SAMPLING_RATIO", "1.0"))
    except ValueError:
        sampling_ratio = 1.0
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": os.getenv("OTEL_SERVICE_NAME", _SERVICE_NAME),
                "deployment.environment.name": os.getenv(
                    "MAP_ENV", "development"
                ),
            }
        ),
        # ParentBased first honors the upstream sampled flag; the ratio only
        # decides root spans minted by the BFF itself.
        sampler=ParentBased(
            root=TraceIdRatioBased(max(0.0, min(sampling_ratio, 1.0)))
        ),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            _build_span_exporter(),
            max_queue_size=int(os.getenv("MAP_OTEL_MAX_QUEUE_SIZE", "2048")),
            max_export_batch_size=int(
                os.getenv("MAP_OTEL_MAX_EXPORT_BATCH_SIZE", "512")
            ),
            # inert in SDK 1.44, but parse via the same helper so a bad
            # MAP_OTEL_EXPORT_TIMEOUT_MS can never crash startup
            export_timeout_millis=int(_export_timeout_seconds() * 1000),
        )
    )
    trace.set_tracer_provider(provider)
    _provider = provider
    instrument_app(app, provider)
    return True


def flush_bff_telemetry() -> None:
    """Force-export buffered spans (used before short-lived shutdowns).

    Safe no-op when nothing was configured or after shutdown.
    """
    if _provider is not None and not _shut_down:
        _provider.force_flush()


def shutdown_bff_telemetry() -> None:
    """Shut the provider down — terminal state, no re-configuration.

    Only call this at process exit. Flush buffered spans first so the tail
    of the batch queue is not lost when the exporter worker stops; then shut
    the provider down. The FastAPI/httpx instrumentation keeps its original
    provider, so after shutdown :func:`configure_bff_telemetry` returns
    ``False`` instead of resurrecting a provider that would never export.
    """
    global _shut_down
    if _provider is None:
        # Nothing was configured: no terminal state to enter.
        return
    _provider.force_flush()
    _provider.shutdown()
    _shut_down = True
