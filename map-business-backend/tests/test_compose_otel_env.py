"""Re-review acceptance tests: OTel compose enablement and pass-through.

Covers, against the merged compose configuration:
  - otel overlay + profile: BOTH services enable OTel with the SAME
    protocol/endpoint pair (default http/protobuf + 4318, gRPC + 4317);
  - base compose alone: NEITHER service enables OTel and no endpoint is set;
  - Round 5 P2-4.1: the emergency kill switch ``OTEL_SDK_DISABLED`` and the
    exporter tuning variables reach BOTH containers with identical values
    (and algorithm-service additionally gets the native-log tuning vars);
  - Round 4 P3: compose runs with ``--env-file /dev/null`` and OTel
    interpolation variables come from the subprocess env only, so the repo
    root ``.env`` can never leak developer-local values into the assertion.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_ENDPOINT = "http://otel-collector:4318"
_GRPC_ENDPOINT = "http://otel-collector:4317"

# Defaults declared in docker-compose.otel.yml (mirror the SDK fallbacks).
_DEFAULT_TUNING = {
    "MAP_OTEL_MAX_QUEUE_SIZE": "2048",
    "MAP_OTEL_MAX_EXPORT_BATCH_SIZE": "512",
    "MAP_OTEL_EXPORT_TIMEOUT_MS": "10000",
    "MAP_OTEL_EXCLUDED_PATHS": "/health,/metrics",
}
# Native-log tuning exists only on algorithm-service. NOTE: the BFF
# currently implements NO OTel logging (neither a native OTLP log exporter
# nor a log-to-span-event bridge) — these vars are map_core-only.
_ALGORITHM_LOG_TUNING = {
    "MAP_OTEL_LOG_MAX_QUEUE_SIZE": "2048",
    "MAP_OTEL_LOG_MAX_EXPORT_BATCH_SIZE": "512",
    "MAP_OTEL_LOG_EXPORT_TIMEOUT_MS": "10000",
    "MAP_OTEL_LOGS_AS_SPAN_EVENTS": "true",
    "MAP_OTEL_LOG_MESSAGE_MAX_CHARS": "4000",
}

# Every var the compose files interpolate, so host values never leak into
# the hermetic subprocess env. Generated from the tuning constants above to
# avoid a second hand-maintained list drifting out of sync (round 7 P2-4.3).
_OTEL_ENV_KEYS = (
    "MAP_OTEL_ENABLED",
    "MAP_OTEL_SAMPLING_RATIO",
    "MAP_OTEL_PROTOCOL",
    "MAP_OTEL_ENDPOINT",
    "OTEL_SDK_DISABLED",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    *tuple(_DEFAULT_TUNING),
    *tuple(_ALGORITHM_LOG_TUNING),
)


def _compose_config(*extra_args: str, env_override: dict[str, str] | None = None) -> dict:
    """Resolve ``docker compose config`` with a hermetic environment.

    Two layers of isolation so the result cannot depend on the developer's
    machine:
    - ``--env-file /dev/null``: compose stops auto-loading the repo root
      ``.env`` (and never falls back to it);
    - OTel interpolation variables are stripped from the inherited env and
      re-injected only from ``env_override``, so declared defaults like
      ``${MAP_OTEL_ENABLED:-false}`` resolve deterministically.
    """
    env = {k: v for k, v in os.environ.items() if k not in _OTEL_ENV_KEYS}
    env.update(env_override or {})
    cmd = [
        "docker",
        "compose",
        "--env-file",
        os.devnull,
        *extra_args,
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"docker compose config failed: {result.stderr.strip()}"
    return json.loads(result.stdout)


def _service_env(config: dict, service: str) -> dict[str, str]:
    environment = config["services"][service].get("environment", {})
    # compose emits environment as either a mapping or a list of KEY=VALUE
    if isinstance(environment, list):
        return dict(item.split("=", 1) for item in environment)
    return {str(key): str(value) for key, value in environment.items()}


@pytest.fixture(scope="module", autouse=True)
def _require_docker():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("docker compose plugin not available")


_OTEL_SERVICES = ("algorithm-service", "backend-service")

_OVERLAY_ARGS = (
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.otel.yml",
    "--profile",
    "otel",
)


def _assert_both_services(
    config: dict,
    *,
    enabled: bool,
    endpoint: str | None,
    protocol: str,
    sdk_disabled: str = "false",
    tuning: dict[str, str] | None = None,
    algorithm_only_tuning: dict[str, str] | None = None,
) -> None:
    for service in _OTEL_SERVICES:
        env = _service_env(config, service)
        assert (env.get("MAP_OTEL_ENABLED") == "true") is enabled, (
            f"{service}: MAP_OTEL_ENABLED mismatch (expected {enabled})"
        )
        assert env.get("OTEL_SDK_DISABLED") == sdk_disabled, (
            f"{service}: OTEL_SDK_DISABLED mismatch (expected {sdk_disabled}); "
            "the emergency kill switch must reach every container"
        )
        if endpoint is None:
            assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env, (
                f"base compose must not set an OTLP endpoint for {service}; "
                "endpoints belong to docker-compose.otel.yml"
            )
        else:
            assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == endpoint, (
                f"{service}: OTLP endpoint mismatch (expected {endpoint})"
            )
        assert env.get("OTEL_EXPORTER_OTLP_PROTOCOL") == protocol, (
            f"{service}: OTLP protocol mismatch (expected {protocol}); "
            "both services must always use the same exporter protocol"
        )
        for key, value in (tuning or {}).items():
            assert env.get(key) == value, (
                f"{service}: {key} mismatch (expected {value!r}); "
                "exporter tuning must reach both services identically"
            )
        for key, value in (algorithm_only_tuning or {}).items():
            expected = value if service == "algorithm-service" else None
            assert env.get(key) == expected, (
                f"{service}: {key} mismatch (expected {expected!r}); "
                "native-log tuning applies to algorithm-service only"
            )


def test_otel_overlay_enables_both_services() -> None:
    config = _compose_config(*_OVERLAY_ARGS)
    _assert_both_services(
        config,
        enabled=True,
        endpoint=_DEFAULT_ENDPOINT,
        protocol="http/protobuf",
        sdk_disabled="false",
        tuning=_DEFAULT_TUNING,
        algorithm_only_tuning=_ALGORITHM_LOG_TUNING,
    )


def test_otel_overlay_grpc_switch_lands_on_both_services() -> None:
    """Round 4 P2-4.1: gRPC must reach both services with the gRPC port.

    Protocol and endpoint are one configuration pair; switching the protocol
    without the matching endpoint would point the gRPC exporter at the HTTP
    listener (4318) and silently break export.
    """
    config = _compose_config(
        *_OVERLAY_ARGS,
        env_override={
            "MAP_OTEL_PROTOCOL": "grpc",
            "MAP_OTEL_ENDPOINT": _GRPC_ENDPOINT,
        },
    )
    _assert_both_services(
        config,
        enabled=True,
        endpoint=_GRPC_ENDPOINT,
        protocol="grpc",
        tuning=_DEFAULT_TUNING,
        algorithm_only_tuning=_ALGORITHM_LOG_TUNING,
    )


def test_emergency_kill_switch_reaches_both_services() -> None:
    """Round 5 P2-4.1: OTEL_SDK_DISABLED=true overrides enablement.

    The SDK priority is OTEL_SDK_DISABLED > MAP_OTEL_ENABLED, so even under
    the otel overlay (MAP_OTEL_ENABLED=true) a kill-switch deployment must
    deliver the disabled flag to both containers unchanged.
    """
    config = _compose_config(
        *_OVERLAY_ARGS,
        env_override={"OTEL_SDK_DISABLED": "true"},
    )
    _assert_both_services(
        config,
        enabled=True,
        endpoint=_DEFAULT_ENDPOINT,
        protocol="http/protobuf",
        sdk_disabled="true",
        tuning=_DEFAULT_TUNING,
        algorithm_only_tuning=_ALGORITHM_LOG_TUNING,
    )


def test_exporter_tuning_reaches_both_services() -> None:
    """Round 5 P2-4.1: non-default tuning values survive the merge.

    Values must satisfy the SDK constraint max_export_batch_size <=
    max_queue_size (round 6 P2-4.2: the previous 64/128 pair was rejected by
    BatchSpanProcessor at startup). 64/32 and the log 64/32 pairs are valid,
    so the same inputs pass compose merge AND SDK initialization.
    """
    override = {
        "MAP_OTEL_MAX_QUEUE_SIZE": "64",
        "MAP_OTEL_MAX_EXPORT_BATCH_SIZE": "32",
        "MAP_OTEL_EXPORT_TIMEOUT_MS": "1234",
        "MAP_OTEL_EXCLUDED_PATHS": "/health,/internal",
        "MAP_OTEL_LOG_MAX_QUEUE_SIZE": "64",
        "MAP_OTEL_LOG_MAX_EXPORT_BATCH_SIZE": "32",
        "MAP_OTEL_LOG_EXPORT_TIMEOUT_MS": "777",
        "MAP_OTEL_LOG_MESSAGE_MAX_CHARS": "2000",
    }
    config = _compose_config(*_OVERLAY_ARGS, env_override=override)
    _assert_both_services(
        config,
        enabled=True,
        endpoint=_DEFAULT_ENDPOINT,
        protocol="http/protobuf",
        tuning={
            "MAP_OTEL_MAX_QUEUE_SIZE": "64",
            "MAP_OTEL_MAX_EXPORT_BATCH_SIZE": "32",
            "MAP_OTEL_EXPORT_TIMEOUT_MS": "1234",
            "MAP_OTEL_EXCLUDED_PATHS": "/health,/internal",
        },
        algorithm_only_tuning={
            "MAP_OTEL_LOG_MAX_QUEUE_SIZE": "64",
            "MAP_OTEL_LOG_MAX_EXPORT_BATCH_SIZE": "32",
            "MAP_OTEL_LOG_EXPORT_TIMEOUT_MS": "777",
            # untouched keys keep their declared defaults
            "MAP_OTEL_LOGS_AS_SPAN_EVENTS": "true",
            "MAP_OTEL_LOG_MESSAGE_MAX_CHARS": "2000",
        },
    )


def test_host_log_vars_do_not_leak_into_compose(monkeypatch) -> None:
    """Round 7 P2-4.3: host values for the new log vars never reach compose.

    Regression for the finding that MAP_OTEL_LOGS_AS_SPAN_EVENTS and
    MAP_OTEL_LOG_MESSAGE_MAX_CHARS were missing from _OTEL_ENV_KEYS: with a
    host value present, the hermetic subprocess env kept it and the overlay
    defaults were overridden. The env filter (generated from the tuning
    constants) must drop them so the declared overlay defaults win.
    """
    monkeypatch.setenv("MAP_OTEL_LOGS_AS_SPAN_EVENTS", "false")
    monkeypatch.setenv("MAP_OTEL_LOG_MESSAGE_MAX_CHARS", "99")
    config = _compose_config(*_OVERLAY_ARGS)
    _assert_both_services(
        config,
        enabled=True,
        endpoint=_DEFAULT_ENDPOINT,
        protocol="http/protobuf",
        tuning=_DEFAULT_TUNING,
        algorithm_only_tuning=_ALGORITHM_LOG_TUNING,
    )


def test_base_compose_keeps_otel_disabled() -> None:
    config = _compose_config("-f", "docker-compose.yml")
    _assert_both_services(
        config,
        enabled=False,
        endpoint=None,
        protocol="http/protobuf",
        sdk_disabled="false",
        # tuning variables belong to the otel overlay, not the base file
        tuning=None,
        algorithm_only_tuning=None,
    )
