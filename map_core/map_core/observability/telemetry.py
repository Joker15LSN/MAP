from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from typing import Any

from loguru import logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter as GrpcOTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http._log_exporter import (
    OTLPLogExporter as HttpOTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpOTLPSpanExporter,
)

# NOTE: ``opentelemetry.sdk._logs.LoggingHandler`` is deprecated since SDK
# 1.44; the supported replacement is the handler from
# ``opentelemetry-instrumentation-logging``. Redaction still happens before
# records reach any handler (see ``loguru_record_to_logging_record``), so the
# sanitizer semantics are unaffected by this swap.
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Tracer

_provider: TracerProvider | None = None
_logger_provider: LoggerProvider | None = None
_logging_handler: LoggingHandler | None = None
_loguru_sink_id: int | None = None
_telemetry_tracer: Tracer | None = None

# Process-lifetime terminal state. The loguru sink cannot be reliably
# reinstalled and, more importantly, re-pointing the global
# TracerProvider/LoggerProvider would NOT re-attach the already-installed
# instrumentation (it keeps its original provider), so after
# shutdown_telemetry() ran, configure_telemetry() returns False instead of
# resurrecting providers that would never export.
_shut_down = False

PHOENIX_PROJECT_RESOURCE_ATTRIBUTE = "openinference.project.name"

_SENSITIVE_KEYS = (
    r"authorization|api[_-]?key|apikey|password|passwd|secret|token|cookie|set-cookie"
)

_SENSITIVE_KEY_PATTERN = re.compile(rf"(?i)({_SENSITIVE_KEYS})")

# JSON-style quoted fields: "api_key": "value" -> keep key, redact value
_SENSITIVE_JSON_PATTERN = re.compile(
    rf"(?i)([\"'](?:{_SENSITIVE_KEYS})[\"']\s*:\s*)[\"'][^\"']*[\"']"
)
# key=value / key: value pairs (URL query, log text, header dumps)
_SENSITIVE_PAIR_PATTERN = re.compile(
    rf"(?i)(?:{_SENSITIVE_KEYS})\s*[=:]\s*(?:bearer\s+)?[\"']?[\w.+~%-][^\s,;\"']*"
)
_SENSITIVE_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[a-z0-9._-]+")
_SENSITIVE_SK_PATTERN = re.compile(r"(?i)\bsk-[a-z0-9_-]+")


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _export_timeout_seconds(env_key: str, default_ms: int = 10000) -> float:
    """Milliseconds from env -> seconds for the OTLP exporter ``timeout``.

    The OTLP exporters take ``timeout`` in seconds; the SDK stores
    BatchSpanProcessor/BatchLogRecordProcessor ``export_timeout_millis`` but
    does NOT wire it into exports (SDK 1.44: "Not used. No way currently to
    pass timeout to export."), so the exporter timeout is the only real
    per-request limit. Non-numeric, NaN/inf or negative values fall back to
    the default / clamp to 0. Use the same helper for the processors'
    (inert) export_timeout_millis so a bad value can never crash startup.
    """
    try:
        millis = float(os.getenv(env_key, str(default_ms)))
    except (TypeError, ValueError):
        millis = float(default_ms)
    if not math.isfinite(millis):
        millis = float(default_ms)
    return max(millis, 0.0) / 1000.0


def _sanitize_log_message(value: Any) -> str:
    """Single redaction entrypoint shared by native logs and span events."""
    message = str(value)
    message = _SENSITIVE_JSON_PATTERN.sub(r'\1"<redacted>"', message)
    message = _SENSITIVE_PAIR_PATTERN.sub("<redacted>", message)
    message = _SENSITIVE_BEARER_PATTERN.sub("bearer <redacted>", message)
    message = _SENSITIVE_SK_PATTERN.sub("sk-<redacted>", message)
    return message[: int(os.getenv("MAP_OTEL_LOG_MESSAGE_MAX_CHARS", "4000"))]


def _sanitize_extra_value(key: str, value: Any) -> Any:
    """Redact structured extras by key blacklist, then by content patterns."""
    if _SENSITIVE_KEY_PATTERN.search(str(key)):
        return "<redacted>"
    if isinstance(value, (bool, int, float)):
        return value
    return _sanitize_log_message(value)


def build_telemetry_resource(
    *,
    service_name: str,
    service_version: str,
    deployment_environment: str,
) -> Resource:
    project_name = os.getenv("PHOENIX_PROJECT_NAME", "map").strip() or "map"
    return Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
            "service.version": os.getenv(
                "MAP_SERVICE_VERSION",
                service_version,
            ),
            "deployment.environment.name": deployment_environment,
            PHOENIX_PROJECT_RESOURCE_ATTRIBUTE: project_name,
        }
    )


def loguru_record_to_logging_record(
    record: Mapping[str, Any],
) -> logging.LogRecord:
    # Native log export must pass through the same redaction as span events.
    extra = {
        f"map.{key}": _sanitize_extra_value(key, value)
        for key, value in record["extra"].items()
        if isinstance(value, (str, bool, int, float))
    }
    log_record = logging.LogRecord(
        name=str(record["name"]),
        level=int(record["level"].no),
        pathname=str(record["file"].path),
        lineno=int(record["line"]),
        msg=_sanitize_log_message(record["message"]),
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(log_record, key, value)
    return log_record


def _loguru_record_to_span_attributes(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "log.severity": str(record["level"].name),
        "log.severity_number": int(record["level"].no),
        "log.body": _sanitize_log_message(record["message"]),
        "code.file.path": str(record["file"].path),
        "code.line.number": int(record["line"]),
        "code.function.name": str(record["function"]),
    }
    for key, value in record["extra"].items():
        if isinstance(value, (str, bool, int, float)):
            attributes[f"map.{key}"] = _sanitize_extra_value(key, value)
    return attributes


def loguru_record_to_span_event(record: Mapping[str, Any]) -> None:
    """Represent a structured log as a Phoenix-visible trace event."""

    attributes = _loguru_record_to_span_attributes(record)
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.add_event("log", attributes=attributes)
        return
    if _telemetry_tracer is None:
        return
    with _telemetry_tracer.start_as_current_span(
        "map.log",
        attributes={
            "log.severity": attributes["log.severity"],
            "code.function.name": attributes["code.function.name"],
        },
    ) as log_span:
        log_span.add_event("log", attributes=attributes)


def configure_telemetry(
    *,
    service_name: str,
    service_version: str,
    deployment_environment: str,
) -> bool:
    global _logger_provider, _logging_handler, _loguru_sink_id, _provider
    global _telemetry_tracer, _shut_down
    if _shut_down:
        # Terminal state — do NOT resurrect providers in this process.
        return False
    if _provider is not None or _logger_provider is not None:
        return True

    traces_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    logs_endpoint = os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    common_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    # Single enablement switch, in priority order:
    #   OTEL_SDK_DISABLED=true > MAP_OTEL_ENABLED=false > enabled + endpoints.
    # Endpoints only configure the export target; they never enable OTel on
    # their own, so a stray endpoint cannot start exporters against a dead
    # collector.
    if _is_truthy(os.getenv("OTEL_SDK_DISABLED")):
        return False
    if not _is_truthy(os.getenv("MAP_OTEL_ENABLED")):
        return False

    trace_protocol = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
    log_protocol = os.getenv(
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
    resource = build_telemetry_resource(
        service_name=service_name,
        service_version=service_version,
        deployment_environment=deployment_environment,
    )
    try:
        sampling_ratio = float(os.getenv("MAP_OTEL_SAMPLING_RATIO", "1.0"))
    except ValueError:
        sampling_ratio = 1.0
    provider = TracerProvider(
        resource=resource,
        # ParentBased honors the upstream sampled flag first, so cross-
        # service traces stay intact even when services configure different
        # ratios; the ratio only decides root spans minted here.
        sampler=ParentBased(
            root=TraceIdRatioBased(max(0.0, min(sampling_ratio, 1.0)))
        ),
    )
    if trace_protocol == "grpc":
        exporter: Any = GrpcOTLPSpanExporter(
            timeout=_export_timeout_seconds("MAP_OTEL_EXPORT_TIMEOUT_MS")
        )
    elif trace_protocol == "http/protobuf":
        exporter = HttpOTLPSpanExporter(
            timeout=_export_timeout_seconds("MAP_OTEL_EXPORT_TIMEOUT_MS")
        )
    else:
        raise ValueError(
            "unsupported OTLP trace protocol; use 'grpc' or 'http/protobuf'"
        )

    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_queue_size=int(os.getenv("MAP_OTEL_MAX_QUEUE_SIZE", "2048")),
            max_export_batch_size=int(
                os.getenv("MAP_OTEL_MAX_EXPORT_BATCH_SIZE", "512")
            ),
            # inert in SDK 1.44, but parse via the same helper so a bad
            # MAP_OTEL_EXPORT_TIMEOUT_MS can never crash startup
            export_timeout_millis=int(
                _export_timeout_seconds("MAP_OTEL_EXPORT_TIMEOUT_MS") * 1000
            ),
        )
    )
    trace.set_tracer_provider(provider)
    _provider = provider

    # Native OTLP log export is opt-in via a logs endpoint or a truthy
    # switch; an explicit "false" switch force-disables it even when a logs
    # endpoint is configured — the bundled Collector stack only runs a
    # traces pipeline, so pointing a logs endpoint at it would export into
    # nothing (use logs-as-span-events there instead).
    native_log_switch = os.getenv("MAP_OTEL_NATIVE_LOG_EXPORT_ENABLED")
    if native_log_switch is not None and not _is_truthy(native_log_switch):
        native_logs_enabled = False
    else:
        native_logs_enabled = bool(logs_endpoint) or _is_truthy(native_log_switch)
    if native_logs_enabled:
        logger_provider = LoggerProvider(resource=resource)
        if log_protocol == "grpc":
            log_exporter: Any = GrpcOTLPLogExporter(
                timeout=_export_timeout_seconds("MAP_OTEL_LOG_EXPORT_TIMEOUT_MS")
            )
        elif log_protocol == "http/protobuf":
            log_exporter = HttpOTLPLogExporter(
                timeout=_export_timeout_seconds("MAP_OTEL_LOG_EXPORT_TIMEOUT_MS")
            )
        else:
            raise ValueError(
                "unsupported OTLP log protocol; use 'grpc' or 'http/protobuf'"
            )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                log_exporter,
                max_queue_size=int(os.getenv("MAP_OTEL_LOG_MAX_QUEUE_SIZE", "2048")),
                max_export_batch_size=int(
                    os.getenv("MAP_OTEL_LOG_MAX_EXPORT_BATCH_SIZE", "512")
                ),
                # inert in SDK 1.44, but parse via the same helper so a bad
                # MAP_OTEL_LOG_EXPORT_TIMEOUT_MS can never crash startup
                export_timeout_millis=int(
                    _export_timeout_seconds("MAP_OTEL_LOG_EXPORT_TIMEOUT_MS")
                    * 1000
                ),
            )
        )
        logging_handler = LoggingHandler(
            level=logging.NOTSET,
            logger_provider=logger_provider,
        )

        _logger_provider = logger_provider
        _logging_handler = logging_handler

    logs_as_span_events = _is_truthy(os.getenv("MAP_OTEL_LOGS_AS_SPAN_EVENTS", "true"))
    if logs_as_span_events and _provider is not None:
        _telemetry_tracer = trace.get_tracer("map.observability.logs")

    if _logging_handler is not None or _telemetry_tracer is not None:

        def emit_loguru(message: Any) -> None:
            if _logging_handler is not None:
                _logging_handler.emit(loguru_record_to_logging_record(message.record))
            if _telemetry_tracer is not None:
                loguru_record_to_span_event(message.record)

        _loguru_sink_id = logger.add(
            emit_loguru,
            level=os.getenv("MAP_OTEL_LOG_LEVEL", "INFO"),
            enqueue=False,
        )

    logger.info(
        "OpenTelemetry enabled: trace_protocol={}, native_logs={}, "
        "logs_as_span_events={}, project={}, endpoint={}",
        trace_protocol,
        native_logs_enabled,
        logs_as_span_events,
        resource.attributes[PHOENIX_PROJECT_RESOURCE_ATTRIBUTE],
        traces_endpoint or logs_endpoint or common_endpoint or "<exporter-default>",
    )
    return True


def get_tracer(name: str) -> Tracer:
    return trace.get_tracer(name)


def current_trace_context() -> dict[str, str]:
    """Return the current span's W3C trace/span ids (empty dict when none).

    Captured at event-emit time (inside the request context) so that async
    event workers can persist trace correlation without live span context.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return {}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


def shutdown_telemetry(timeout_millis: int = 5000) -> None:
    """Shut all providers down — terminal state, no re-configuration.

    Only call this at process exit (e.g. from the FastAPI lifespan finally
    block). Flush before shutdown so the tail of the batch queues is not
    lost when the exporter workers stop. Re-pointing the global
    TracerProvider/LoggerProvider afterwards would NOT re-attach the
    already-installed instrumentation, so :func:`configure_telemetry`
    returns ``False`` instead of resurrecting providers that would never
    export. Provider references are kept so the module state reflects the
    real (shut-down) object rather than pretending telemetry never existed.
    """
    global _logger_provider, _logging_handler, _loguru_sink_id, _provider
    global _telemetry_tracer, _shut_down
    if _loguru_sink_id is not None:
        logger.remove(_loguru_sink_id)
        _loguru_sink_id = None
    if _logger_provider is not None:
        _logger_provider.force_flush(timeout_millis=timeout_millis)
        _logger_provider.shutdown()
        _logging_handler = None
    _telemetry_tracer = None
    provider = _provider
    if provider is None:
        # Nothing was configured: no terminal state to enter.
        return
    provider.force_flush(timeout_millis=timeout_millis)
    provider.shutdown()
    _shut_down = True
