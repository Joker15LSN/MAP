"""R2-P2-04 / R3-P2-01: ops scripts must run with their documented commands.

The second-round review found ``scripts/verify_*.py`` could not import
``app`` when run as documented from a clean checkout (script execution puts
only scripts/ on sys.path). R3-P2-01 additionally requires the FULL
subprocess matrix: every documented invocation form (direct file and
``-m`` module) of all three ops scripts, plus their documented success AND
failure exit codes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import APP_DSN, MIGRATION_DSN

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Documented invocation forms per script (docstring "Usage" of each script),
# with the environment variable each script reads its DSN from and a marker
# present in the success output.
SCRIPT_CASES = [
    pytest.param(
        "scripts/verify_audit_chain.py",
        "scripts.verify_audit_chain",
        "MAP_CONTROL_DB_DSN",
        APP_DSN,
        "CHAIN_OK",
        id="verify_audit_chain",
    ),
    pytest.param(
        "scripts/verify_feedback_backfill.py",
        "scripts.verify_feedback_backfill",
        "MAP_CONTROL_DB_DSN",
        APP_DSN,
        "legacy_rows=",
        id="verify_feedback_backfill",
    ),
    pytest.param(
        "scripts/quarantine_audit_chain.py",
        "scripts.quarantine_audit_chain",
        "MAP_CONTROL_MIGRATION_DSN",
        MIGRATION_DSN,
        "CHAIN_OK",
        id="quarantine_audit_chain",
    ),
]


def _run(args: list[str], extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env}
    return subprocess.run(
        [sys.executable, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize("form", ["direct", "module"])
@pytest.mark.parametrize("script_path,module_name,dsn_var,dsn_value,marker", SCRIPT_CASES)
def test_ops_script_documented_form_succeeds(
    _engine, form: str, script_path: str, module_name: str,
    dsn_var: str, dsn_value: str, marker: str,
) -> None:
    """Every documented invocation form of every ops script exits 0 on a
    healthy integration database (chain verified OK, no legacy conflicts,
    nothing to quarantine)."""
    args = [script_path] if form == "direct" else ["-m", module_name]
    result = _run(args, {dsn_var: dsn_value})
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout


@pytest.mark.parametrize(
    "script_path,dsn_var",
    [
        pytest.param(
            "scripts/verify_audit_chain.py",
            "MAP_CONTROL_DB_DSN",
            id="verify_audit_chain",
        ),
        pytest.param(
            "scripts/verify_feedback_backfill.py",
            "MAP_CONTROL_DB_DSN",
            id="verify_feedback_backfill",
        ),
    ],
)
def test_ops_script_unreachable_dsn_fails(script_path: str, dsn_var: str) -> None:
    """Failure exit code: an unreachable database must exit non-zero with a
    connection error, never silently report success."""
    result = _run(
        [script_path],
        {dsn_var: "postgresql+asyncpg://map:map@127.0.0.1:1/map"},
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CHAIN_OK" not in combined
    assert "legacy_rows=" not in combined


def test_quarantine_missing_migration_dsn_fails_closed() -> None:
    """quarantine_audit_chain documents exit 2 when the migration DSN is
    absent; it must fail closed without touching the chain."""
    env_overrides = {
        key: value
        for key, value in os.environ.items()
        if key != "MAP_CONTROL_MIGRATION_DSN"
    }
    result = subprocess.run(
        [sys.executable, "scripts/quarantine_audit_chain.py"],
        cwd=PROJECT_ROOT,
        env=env_overrides,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "MAP_CONTROL_MIGRATION_DSN" in result.stdout + result.stderr
