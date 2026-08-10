"""R3-P2-04: machine-readable supply-chain exception register.

Proves ``scripts/load_dependency_exceptions.py`` enforces the documented
expiry rule automatically:

- the committed register parses and is currently empty (no advisory is
  allowlisted);
- a valid, unexpired entry yields its advisory ID;
- missing fields / malformed dates / future approvals fail closed;
- an EXPIRED entry fails the gate (exit 2) — the automated failure test
  required by the review.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER = REPO_ROOT / "scripts" / "load_dependency_exceptions.py"
REGISTER = REPO_ROOT / "security" / "dependency_exceptions.json"


def _run_loader(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LOADER), str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _valid_entry(approved: date, expires: date) -> dict:
    return {
        "exception_id": "SEC-EX-001",
        "advisory": "PYSEC-2099-1",
        "package": "example-pkg",
        "introduction": "direct dependency",
        "reachability": "affected API not reachable; evidence in ticket",
        "mitigation": "WAF rule X deployed",
        "why_not_upgraded": "upstream fix not released yet",
        "owner": "alice",
        "ticket": "TICKET-123",
        "approver": "bob",
        "approved_at": approved.isoformat(),
        "expires": expires.isoformat(),
    }


def _write(tmp_path: Path, exceptions: list) -> Path:
    file = tmp_path / "exceptions.json"
    file.write_text(
        json.dumps({"version": 1, "exceptions": exceptions}), encoding="utf-8"
    )
    return file


def test_committed_register_is_valid_and_empty() -> None:
    """The shipped register must parse cleanly; it is currently empty, so
    no advisory may be allowlisted anywhere in the gate."""
    result = _run_loader(REGISTER)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""


def test_valid_unexpired_entry_emits_advisory(tmp_path) -> None:
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "PYSEC-2099-1"


def test_expired_entry_fails_the_gate(tmp_path) -> None:
    """Automated expiry failure test: an exception whose expiry date has
    passed must fail the gate exactly like a new vulnerability."""
    today = date.today()
    entry = _valid_entry(today - timedelta(days=40), today - timedelta(days=10))
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 2
    assert "expired" in result.stderr
    assert "PYSEC-2099-1" in result.stderr


def test_expiry_exactly_today_fails_the_gate(tmp_path) -> None:
    today = date.today()
    entry = _valid_entry(today - timedelta(days=30), today)
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 2


def test_missing_required_fields_fail_closed(tmp_path) -> None:
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    del entry["owner"]
    entry["ticket"] = "   "
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 1
    assert "owner" in result.stderr
    assert "ticket" in result.stderr


def test_malformed_date_fails_closed(tmp_path) -> None:
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    entry["expires"] = "30/09/2026"
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 1
    assert "ISO" in result.stderr


def test_future_approval_and_reversed_window_fail_closed(tmp_path) -> None:
    today = date.today()
    future = _valid_entry(today + timedelta(days=2), today + timedelta(days=40))
    result = _run_loader(_write(tmp_path, [future]))
    assert result.returncode == 1
    assert "future" in result.stderr

    reversed_window = _valid_entry(today - timedelta(days=1), today - timedelta(days=2))
    result = _run_loader(_write(tmp_path, [reversed_window]))
    assert result.returncode == 1


def test_duplicate_advisory_fails_closed(tmp_path) -> None:
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    duplicate = dict(entry)
    duplicate["exception_id"] = "SEC-EX-002"
    result = _run_loader(_write(tmp_path, [entry, duplicate]))
    assert result.returncode == 1
    assert "duplicates" in result.stderr
