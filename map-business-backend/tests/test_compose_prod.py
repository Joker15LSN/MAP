"""S4-06 acceptance: production Compose is fail-closed on MAP_ENV.

S5-03 [P0]: production Compose injects the CORS contract into the BFF.

Verifies the production override (docker-compose.prod.yml) against the
merged compose configuration:

- MAP_ENV unset -> docker compose config exits non-zero (the :? required
  interpolation in the override);
- MAP_ENV=prod -> all three application services resolve to prod;
- the production entrypoint scripts/compose-prod.sh exits non-zero for
  MAP_ENV=dev / MAP_ENV=<unknown> (no silent dev fallback);
- S5-03: the prod merge resolves MAP_CORS_ORIGINS / MAP_CORS_ALLOW_CREDENTIALS
  into BOTH backend-service and algorithm-service (origins :?required,
  credentials default false), and the BFF startup path (create_app) honours
  those container env values fail-closed.

Hermetic like the OTel compose test: --env-file /dev/null and a stripped
subprocess env so the developer's .env / shell can never leak in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]

# The three application services whose MAP_ENV must resolve to prod.
_PROD_SERVICES = ("algorithm-service", "backend-service", "worker-service")

# Compose interpolation vars we must control hermeticly.
_STRIP_KEYS = (
    "MAP_ENV",
    "MAP_CORS_ORIGINS",
    "MAP_CORS_ALLOW_CREDENTIALS",
    # S6-03: the prod override :?requires the sandbox credential registry.
    "MAP_SANDBOX_SERVICE_CREDENTIALS",
    "MAP_SANDBOX_SERVICE_AUDIENCE",
    "MAP_SANDBOX_CORE_TOKEN",
)

# S6-03 / S7-04: prod compose tests must inject a (fake) credential
# registry so the :? interpolation succeeds; the value itself is asserted
# separately. S7-04 makes expires_at mandatory.
_SANDBOX_CREDENTIALS = (
    '[{"key_id":"k-test","token":"fake-sandbox-token",'
    '"service_name":"map-worker","audience":"map-core",'
    '"scopes":["sandbox:execute"],'
    '"expires_at":"2099-12-31T23:59:59Z"}]'
)

# P0-SEC-01: base compose passwords are :?required; inject fake one-shot
# values so the config assertion exercises MAP_ENV, not password defaults.
_FAKE_COMPOSE_CREDENTIALS = {
    "MAP_POSTGRES_ADMIN_PASSWORD": "fake-admin-pw-for-compose-test",
    "MAP_POSTGRES_APP_PASSWORD": "fake-app-pw-for-compose-test",
    "MAP_POSTGRES_MIGRATOR_PASSWORD": "fake-migrator-pw-for-compose-test",
    "MAP_MONGO_ROOT_PASSWORD": "fake-mongo-pw-for-compose-test",
}

_OVERRIDE_ARGS = ("-f", "docker-compose.yml", "-f", "docker-compose.prod.yml")


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


def _env(env_override: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_KEYS}
    for key in _FAKE_COMPOSE_CREDENTIALS:
        env.pop(key, None)
    env.update(_FAKE_COMPOSE_CREDENTIALS)
    # S6-03: satisfy the prod override's :?required credential registry by
    # default so CORS/MAP_ENV assertions stay focused (a test that targets
    # the registry itself overrides this explicitly).
    env.setdefault("MAP_SANDBOX_SERVICE_CREDENTIALS", _SANDBOX_CREDENTIALS)
    env.update(env_override or {})
    return env


def _compose(
    *extra_args: str,
    env_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
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
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_env(env_override),
        check=False,
    )


def _entrypoint(
    *args: str,
    env_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [str(REPO_ROOT / "scripts" / "compose-prod.sh"), *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_env(env_override),
        check=False,
    )


def _compose_config(
    *extra_args: str,
    env_override: dict[str, str] | None = None,
) -> dict:
    """Resolve docker compose config and return the parsed JSON."""
    result = _compose(*extra_args, env_override=env_override)
    assert result.returncode == 0, f"docker compose config failed: {result.stderr.strip()}"
    return json.loads(result.stdout)


def _service_env(config: dict, service: str) -> dict[str, str]:
    environment = config["services"][service].get("environment", {})
    # compose emits environment as either a mapping or a list of KEY=VALUE.
    if isinstance(environment, list):
        return dict(item.split("=", 1) for item in environment)
    return {str(key): str(value) for key, value in environment.items()}


_CORS_SERVICES = ("backend-service", "algorithm-service")

_AUTH_SECRET = "fake-proxy-secret-for-cors-compose-test"

# Every Settings-relevant env var, dropped before injecting the container env
# so a developer shell can never leak into the in-process startup assertions.
_STARTUP_CLEAN_KEYS = (
    "MAP_CORE_API_ORIGIN",
    "MAP_BFF_STATE_FILE",
    "MAP_AUTH_MODE",
    "MAP_ENV",
    "MAP_DEFAULT_WORKSPACE_ID",
    "MAP_TRUSTED_PROXY_SECRET",
    "MAP_TRUSTED_PROXY_REQUIRED",
    "MAP_SERVICE_CREDENTIALS",
    "MAP_SERVICE_AUDIENCE",
    "MAP_CORS_ORIGINS",
    "MAP_CORS_ALLOW_CREDENTIALS",
)


def _backend_env(
    *files: str,
    env_override: dict[str, str] | None = None,
) -> dict[str, str]:
    config = _compose_config(*files, env_override=env_override)
    return _service_env(config, "backend-service")


def _inject_compose_env(
    monkeypatch,
    tmp_path,
    backend_env: dict[str, str],
    *,
    trusted_header: bool = True,
    cors_origins: str | None = None,
    cors_allow_credentials: str | None = None,
    env: str | None = None,
) -> None:
    """Replay the backend container env (from compose config) in-process.

    create_app() reads load_settings() from os.environ exactly as the
    container would, so the resolved compose values are injected here. Auth
    is set to a valid trusted_header identity for prod cases: S5-03 covers
    CORS only and the prod override does not (yet) pin MAP_AUTH_MODE, so the
    unrelated MAP_AUTH_MODE=dev fail-closed must not mask the CORS path.
    """
    for key in _STARTUP_CLEAN_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAP_ENV", env if env is not None else backend_env.get("MAP_ENV", "dev"))
    monkeypatch.setenv(
        "MAP_CORS_ORIGINS",
        cors_origins if cors_origins is not None else backend_env.get("MAP_CORS_ORIGINS", "*"),
    )
    monkeypatch.setenv(
        "MAP_CORS_ALLOW_CREDENTIALS",
        cors_allow_credentials
        if cors_allow_credentials is not None
        else backend_env.get("MAP_CORS_ALLOW_CREDENTIALS", "true"),
    )
    monkeypatch.setenv("MAP_BFF_STATE_FILE", str(tmp_path / "admin_state.json"))
    if trusted_header:
        monkeypatch.setenv("MAP_AUTH_MODE", "trusted_header")
        monkeypatch.setenv("MAP_TRUSTED_PROXY_REQUIRED", "true")
        monkeypatch.setenv("MAP_TRUSTED_PROXY_SECRET", _AUTH_SECRET)
    else:
        monkeypatch.setenv("MAP_AUTH_MODE", "dev")


def test_prod_compose_missing_map_env_exits_nonzero() -> None:
    # S5-03: inject origins so the only remaining :? failure is MAP_ENV
    # (otherwise compose reports the alphabetically-first MAP_CORS_ORIGINS).
    result = _compose(
        *_OVERRIDE_ARGS,
        env_override={"MAP_CORS_ORIGINS": "https://app.example.com"},
    )
    assert result.returncode != 0
    assert "MAP_ENV" in result.stderr


def test_prod_compose_map_env_prod_resolves_all_three_services() -> None:
    result = _compose(
        *_OVERRIDE_ARGS,
        env_override={
            "MAP_ENV": "prod",
            # S5-03: the prod override now :?requires origins; inject a legal
            # value so this assertion exercises the MAP_ENV signal, not CORS.
            "MAP_CORS_ORIGINS": "https://app.example.com",
        },
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    for service in _PROD_SERVICES:
        environment = config["services"][service].get("environment", {})
        if isinstance(environment, list):
            environment = dict(item.split("=", 1) for item in environment)
        assert environment.get("MAP_ENV") == "prod", (
            f"{service}: MAP_ENV must resolve to prod, got "
            f"{environment.get('MAP_ENV')!r}"
        )
    # map_core also mirrors the legacy ENV reader to prod.
    algorithm_env = config["services"]["algorithm-service"].get("environment", {})
    if isinstance(algorithm_env, list):
        algorithm_env = dict(item.split("=", 1) for item in algorithm_env)
    assert algorithm_env.get("ENV") == "prod"


def test_prod_entrypoint_rejects_dev_and_unknown() -> None:
    for bad in ("dev", "staging"):
        result = _entrypoint("config", env_override={"MAP_ENV": bad})
        assert result.returncode != 0, f"MAP_ENV={bad} must be rejected"
        assert "MAP_ENV" in result.stderr


def test_prod_entrypoint_accepts_prod() -> None:
    result = _entrypoint(
        "config",
        env_override={
            "MAP_ENV": "prod",
            # S5-03: origins are :?required in the prod override.
            "MAP_CORS_ORIGINS": "https://app.example.com",
        },
    )
    assert result.returncode == 0, result.stderr


# --- S5-03 [P0]: production Compose injects the CORS contract into the BFF ---


def test_prod_compose_injects_cors_into_both_services() -> None:
    """The prod merge resolves CORS into BOTH containers (no internal fallback).

    Credentials default to false under the prod override, so a deploy that
    sets only origins never inherits the base dev wildcard + credentials.
    """
    config = _compose_config(
        *_OVERRIDE_ARGS,
        env_override={"MAP_ENV": "prod", "MAP_CORS_ORIGINS": "https://app.example.com"},
    )
    for service in _CORS_SERVICES:
        env = _service_env(config, service)
        assert env["MAP_CORS_ORIGINS"] == "https://app.example.com", service
        assert env["MAP_CORS_ALLOW_CREDENTIALS"] == "false", service


def test_prod_compose_missing_cors_origins_exits_nonzero() -> None:
    result = _compose(*_OVERRIDE_ARGS, env_override={"MAP_ENV": "prod"})
    assert result.returncode != 0
    assert "MAP_CORS_ORIGINS" in result.stderr


def test_prod_compose_explicit_cors_credentials_reach_both_services() -> None:
    config = _compose_config(
        *_OVERRIDE_ARGS,
        env_override={
            "MAP_ENV": "prod",
            "MAP_CORS_ORIGINS": "https://app.example.com",
            "MAP_CORS_ALLOW_CREDENTIALS": "true",
        },
    )
    for service in _CORS_SERVICES:
        env = _service_env(config, service)
        assert env["MAP_CORS_ORIGINS"] == "https://app.example.com", service
        assert env["MAP_CORS_ALLOW_CREDENTIALS"] == "true", service


def test_base_compose_keeps_dev_cors_defaults() -> None:
    config = _compose_config("-f", "docker-compose.yml", env_override={"MAP_ENV": "dev"})
    for service in _CORS_SERVICES:
        env = _service_env(config, service)
        assert env["MAP_CORS_ORIGINS"] == "*", service
        assert env["MAP_CORS_ALLOW_CREDENTIALS"] == "true", service


# --- S6-03: the sandbox execution surface stays off the host ----------------

def test_prod_compose_removes_algorithm_service_host_port() -> None:
    """Production publishes NO host port for map_core: the privileged
    /sandbox/exec surface is reachable only on the private compose
    network."""
    config = _compose_config(
        *_OVERRIDE_ARGS,
        env_override={"MAP_ENV": "prod", "MAP_CORS_ORIGINS": "https://app.example.com"},
    )
    algo_ports = config["services"]["algorithm-service"].get("ports") or []
    assert algo_ports == [], f"algorithm-service publishes ports in prod: {algo_ports}"


def test_base_compose_binds_algo_port_to_loopback_only() -> None:
    """The dev port is bound to 127.0.0.1 (defense in depth: never on
    all interfaces in any environment)."""
    config = _compose_config("-f", "docker-compose.yml", env_override={"MAP_ENV": "dev"})
    algo_ports = config["services"]["algorithm-service"].get("ports") or []
    assert algo_ports, "base compose must publish the dev algo port"
    for entry in algo_ports:
        host_ip = entry.get("host_ip") or entry.get("published", "").split(":", 1)[0]
        assert "127.0.0.1" in str(host_ip), f"dev algo port not loopback-bound: {entry}"


def test_prod_compose_requires_sandbox_service_credentials() -> None:
    """Missing the credential registry fails the prod config interpolation
    (fail-closed before startup)."""
    result = _compose(
        *_OVERRIDE_ARGS,
        env_override={
            "MAP_ENV": "prod",
            "MAP_CORS_ORIGINS": "https://app.example.com",
            "MAP_SANDBOX_SERVICE_CREDENTIALS": "",
        },
    )
    assert result.returncode != 0
    assert "MAP_SANDBOX_SERVICE_CREDENTIALS" in result.stderr


def test_prod_compose_injects_sandbox_credentials_into_algo() -> None:
    config = _compose_config(
        *_OVERRIDE_ARGS,
        env_override={"MAP_ENV": "prod", "MAP_CORS_ORIGINS": "https://app.example.com"},
    )
    env = _service_env(config, "algorithm-service")
    assert "sandbox:execute" in env["MAP_SANDBOX_SERVICE_CREDENTIALS"]


# --- S5-03 startup path: create_app honours the resolved container env -----


def test_startup_prod_legal_origin_succeeds_and_enforces_preflight(
    monkeypatch, tmp_path
) -> None:
    backend_env = _backend_env(
        *_OVERRIDE_ARGS,
        env_override={
            "MAP_ENV": "prod",
            "MAP_CORS_ORIGINS": "https://app.example.com",
            "MAP_CORS_ALLOW_CREDENTIALS": "true",
        },
    )
    _inject_compose_env(monkeypatch, tmp_path, backend_env)
    app = create_app()
    client = TestClient(app)

    allowed = client.options(
        "/ready",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"

    denied = client.options(
        "/ready",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_startup_prod_empty_origins_fails(monkeypatch, tmp_path) -> None:
    # Compose :? already blocks an empty MAP_CORS_ORIGINS at config time; this
    # drives the startup path directly with the same value the BFF would see.
    _inject_compose_env(
        monkeypatch,
        tmp_path,
        {},
        cors_origins="",
        env="prod",
    )
    with pytest.raises(RuntimeError, match="invalid MAP_CORS_ORIGINS"):
        create_app()


def test_startup_prod_wildcard_with_credentials_fails(monkeypatch, tmp_path) -> None:
    backend_env = _backend_env(
        *_OVERRIDE_ARGS,
        env_override={
            "MAP_ENV": "prod",
            "MAP_CORS_ORIGINS": "*",
            "MAP_CORS_ALLOW_CREDENTIALS": "true",
        },
    )
    _inject_compose_env(monkeypatch, tmp_path, backend_env)
    with pytest.raises(RuntimeError, match="wildcard CORS with credentials"):
        create_app()


def test_startup_prod_illegal_origin_fails(monkeypatch, tmp_path) -> None:
    backend_env = _backend_env(
        *_OVERRIDE_ARGS,
        env_override={
            "MAP_ENV": "prod",
            "MAP_CORS_ORIGINS": "https://app.example.com/path",
            "MAP_CORS_ALLOW_CREDENTIALS": "false",
        },
    )
    _inject_compose_env(monkeypatch, tmp_path, backend_env)
    with pytest.raises(RuntimeError, match="invalid MAP_CORS_ORIGINS"):
        create_app()


def test_startup_dev_defaults_succeed(monkeypatch, tmp_path) -> None:
    backend_env = _backend_env("-f", "docker-compose.yml", env_override={"MAP_ENV": "dev"})
    _inject_compose_env(monkeypatch, tmp_path, backend_env, trusted_header=False)
    app = create_app()
    assert app.state.settings.cors_origins == "*"
    assert app.state.settings.cors_allow_credentials is True
