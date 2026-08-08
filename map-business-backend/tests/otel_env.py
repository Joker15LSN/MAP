"""Single source of truth for OTel env vars the BFF reads.

Shared by ``conftest.py`` (hermetic pytest guard) and the telemetry tests
(clean_telemetry fixture + subprocess env filtering), so the pytest process
and the subprocess probes always agree on what must be isolated from the
developer's shell / CI runner.

Keep this list in sync with every ``os.getenv`` in ``app/telemetry.py`` and
with the instrumentation env knobs (``OTEL_PYTHON_EXCLUDED_URLS``).
"""

from __future__ import annotations

OTEL_ENV_VARS: tuple[str, ...] = (
    # enablement / kill switch
    "MAP_OTEL_ENABLED",
    "OTEL_SDK_DISABLED",
    # endpoints (common + per-signal)
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    # protocols
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    # sampling / identity
    "MAP_OTEL_SAMPLING_RATIO",
    "OTEL_SERVICE_NAME",
    "MAP_ENV",
    # exporter tuning
    "MAP_OTEL_MAX_QUEUE_SIZE",
    "MAP_OTEL_MAX_EXPORT_BATCH_SIZE",
    "MAP_OTEL_EXPORT_TIMEOUT_MS",
    # instrumentation exclusions (BFF reads MAP_OTEL_EXCLUDED_PATHS; the
    # standard OTEL_PYTHON_EXCLUDED_URLS and the FastAPI-specific
    # OTEL_PYTHON_FASTAPI_EXCLUDED_URLS are inert for the BFF because
    # instrument_app always passes an explicit excluded_urls, but we still
    # drop them so a host value can never influence SDK defaults elsewhere;
    # OTEL_PYTHON_HTTPX_EXCLUDED_URLS DOES affect the installed httpx
    # instrumentation directly and must be isolated too)
    "MAP_OTEL_EXCLUDED_PATHS",
    "OTEL_PYTHON_EXCLUDED_URLS",
    "OTEL_PYTHON_HTTPX_EXCLUDED_URLS",
    "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
)
