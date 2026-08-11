"""Fresh-volume tests for ``db/init/01-roles.sh`` (R3-P2-03).

Proves the role init script survives REAL production-style secrets:
passwords containing spaces, single quotes, dollar signs and double
quotes must not break the SQL (``format('%I')`` / ``format('%L')``
quoting), the resulting roles must log in with exactly that password,
and the app role must never be superuser. Also proves invalid role
names fail closed WITHOUT printing any secret material.

Requires docker (skipped when unavailable). Each case boots its own
throwaway postgres:16 container on a fresh anonymous volume.
"""

from __future__ import annotations

import secrets
import subprocess
import time
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "map-business-backend" / "db" / "init" / "01-roles.sh"

TRICKY_PASSWORDS = [
    pytest.param("pa ss word", id="space"),
    pytest.param("it's-a-secret", id="single-quote"),
    pytest.param("pa$$word$HOME", id="dollar"),
    pytest.param("mix'ed \"$ecret\" \\x", id="combined"),
]


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30.0
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker is not available"
)


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _wait_ready(container: str, timeout_s: float = 120.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = _run(
            ["docker", "exec", container, "pg_isready", "-U", "map_admin", "-d", "map"],
            timeout=30.0,
        )
        if probe.returncode == 0:
            return
        time.sleep(2.0)
    logs = _run(["docker", "logs", container], timeout=60.0)
    raise AssertionError(
        f"postgres container {container} never became ready:\n"
        f"{(logs.stdout + logs.stderr)[-3000:]}"
    )


@pytest.mark.parametrize("password", TRICKY_PASSWORDS)
@requires_docker
def test_fresh_volume_init_accepts_tricky_passwords(password: str) -> None:
    container = f"map-roles-init-{secrets.token_hex(4)}"
    try:
        started = _run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                container,
                "-e",
                "POSTGRES_USER=map_admin",
                "-e",
                "POSTGRES_PASSWORD=map-admin-local",
                "-e",
                "POSTGRES_DB=map",
                "-e",
                f"MAP_POSTGRES_APP_PASSWORD={password}",
                "-e",
                f"MAP_POSTGRES_MIGRATOR_PASSWORD={password}",
                "-v",
                f"{SCRIPT_PATH}:/docker-entrypoint-initdb.d/01-roles.sh:ro",
                "postgres:16",
            ]
        )
        assert started.returncode == 0, f"docker run failed: {started.stderr}"
        _wait_ready(container)

        # The init script itself must have succeeded (entrypoint aborts on
        # any initdb.d failure, which would also fail pg_isready — but keep
        # the explicit evidence for the report).
        init_logs = _run(["docker", "logs", container])
        assert "map roles initialised" in init_logs.stdout + init_logs.stderr

        # Real password authentication over TCP (host entries in pg_hba
        # require the password; the local socket would be trust).
        dsn = f"postgresql://map:{urllib.parse.quote(password, safe='')}@127.0.0.1:5432/map"
        login = _run(
            ["docker", "exec", container, "psql", dsn, "-Atc", "SELECT current_user"]
        )
        assert login.returncode == 0, f"app role login failed: {login.stderr}"
        assert login.stdout.strip() == "map"

        # Privilege contract: app role never superuser, schema owned by
        # the migrator, app role has usage on map_control.
        admin = ["docker", "exec", container, "psql", "-U", "map_admin", "-d", "map", "-Atc"]
        superuser = _run([*admin, "SELECT rolsuper FROM pg_roles WHERE rolname = 'map'"])
        assert superuser.stdout.strip() == "f"
        schema_owner = _run(
            [
                *admin,
                "SELECT nspowner::regrole::text FROM pg_namespace "
                "WHERE nspname = 'map_control'",
            ]
        )
        assert schema_owner.stdout.strip() == "map_migrator"
        usage = _run(
            [
                *admin,
                "SELECT has_schema_privilege('map', 'map_control', 'USAGE')",
            ]
        )
        assert usage.stdout.strip() == "t"
    finally:
        _run(["docker", "rm", "-f", container], timeout=60.0)


def test_invalid_role_name_fails_closed_without_leaking_secrets() -> None:
    """The script must reject non-simple role names BEFORE any SQL runs and
    without echoing password values."""
    env_password = "super-secret-$value"
    result = subprocess.run(
        ["bash", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        timeout=30.0,
        env={
            "PATH": "/usr/bin:/bin",
            "MAP_POSTGRES_APP_USER": "bad;name",
            "MAP_POSTGRES_APP_PASSWORD": env_password,
            "POSTGRES_USER": "map_admin",
            "POSTGRES_DB": "map",
        },
    )
    assert result.returncode == 1
    assert "must match" in result.stderr
    assert env_password not in result.stderr + result.stdout
    assert "super-secret" not in result.stderr + result.stdout
