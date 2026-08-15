"""S4-06 acceptance: production Compose is fail-closed on MAP_ENV.

Verifies the production override (docker-compose.prod.yml) against the
merged compose configuration:

- MAP_ENV unset -> docker compose config exits non-zero (the :? required
  interpolation in the override);
- MAP_ENV=prod -> all three application services resolve to prod;
- the production entrypoint scripts/compose-prod.sh exits non-zero for
  MAP_ENV=dev / MAP_ENV=<unknown> (no silent dev fallback).

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

REPO_ROOT = Path(__file__).resolve().parents[2]

# The three application services whose MAP_ENV must resolve to prod.
_PROD_SERVICES = ("algorithm-service", "backend-service", "worker-service")

# Compose interpolation vars we must control hermeticly.
_STRIP_KEYS = (
    "MAP_ENV",
    "MAP_CORS_ORIGINS",
    "MAP_CORS_ALLOW_CREDENTIALS",
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


def test_prod_compose_missing_map_env_exits_nonzero() -> None:
    result = _compose(*_OVERRIDE_ARGS)
    assert result.returncode != 0
    assert "MAP_ENV" in result.stderr


def test_prod_compose_map_env_prod_resolves_all_three_services() -> None:
    result = _compose(*_OVERRIDE_ARGS, env_override={"MAP_ENV": "prod"})
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
    result = _entrypoint("config", env_override={"MAP_ENV": "prod"})
    assert result.returncode == 0, result.stderr
