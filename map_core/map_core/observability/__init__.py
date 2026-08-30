"""MAP OpenTelemetry observability package.

Provides TracerProvider configuration (OTLP grpc/http), log-to-span-event
bridging and a minimal ASGI SERVER span middleware. Coexists with the typed
execution event stream; it never replaces it.
"""

from .asgi import OpenTelemetryASGIMiddleware
from .telemetry import (
    configure_telemetry,
    current_trace_context,
    get_tracer,
    loguru_record_to_span_event,
    shutdown_telemetry,
)

__all__ = [
    "OpenTelemetryASGIMiddleware",
    "configure_telemetry",
    "current_trace_context",
    "get_tracer",
    "loguru_record_to_span_event",
    "shutdown_telemetry",
]
