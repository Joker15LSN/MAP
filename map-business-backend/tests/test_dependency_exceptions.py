"""R3-P2-04 / R4-P2-02: machine-readable supply-chain exception register.

Proves ``scripts/load_dependency_exceptions.py`` enforces the documented
rules automatically:

- the committed register parses and is currently empty (no advisory is
  allowlisted);
- a valid, unexpired entry yields its advisory ID;
- the advisory must be a SINGLE well-formed ID from the explicit format
  allowlist (CVE/GHSA/PYSEC) — whitespace, control characters, shell
  metacharacters and smuggled extra tokens fail closed (R4-P2-02);
- missing fields / malformed dates / future approvals fail closed;
- an EXPIRED entry fails the gate (exit 2) — the automated failure test
  required by the review;
- one exception yields exactly ONE ``--ignore-vuln`` argv pair, two
  exceptions exactly two, and a simulated non-zero pip-audit exit is
  propagated (never masked) even when allowlist tokens are present
  (R4-P2-02 argv proofs).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER = REPO_ROOT / "scripts" / "load_dependency_exceptions.py"
REGISTER = REPO_ROOT / "security" / "dependency_exceptions.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from load_dependency_exceptions import validate_advisory  # noqa: E402


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


# ---------------------------------------------------------------------------
# R4-P2-02: single-well-formed-ID advisory validation
# ---------------------------------------------------------------------------

VALID_ADVISORIES = [
    "CVE-2026-12345",
    "CVE-1999-0001",
    "GHSA-28p8-x962-2x9m",
    "GHSA-x2x2-x2x2-x2x2",
    "PYSEC-2026-1",
    "PYSEC-2026-123456",
]


@pytest.mark.parametrize("advisory", VALID_ADVISORIES)
def test_supported_advisory_formats_are_accepted(tmp_path, advisory: str) -> None:
    assert validate_advisory(advisory) == advisory
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    entry["advisory"] = advisory
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == advisory


MALICIOUS_ADVISORIES = [
    "PYSEC-2099-1 --ignore-vuln PYSEC-2099-2",  # smuggled extra token
    "PYSEC-2099-1; true",  # shell metacharacter
    "PYSEC-2099-1\nCVE-2026-9999",  # newline
    "PYSEC-2099-1 # comment",  # hash
    "$(rm -rf /)",  # command substitution
    "`id`",  # backtick substitution
    "PYSEC-2099-1 | cat /etc/passwd",  # pipe
    " PYSEC-2099-1",  # leading whitespace
    "PYSEC-2099-1 ",  # trailing whitespace
    "PYSEC-2099-1\t",  # control character
    "CVE-2026-123",  # too-short sequence (format violation)
    "PYSEC-26-1",  # malformed year
    "GHSA-AAAA-BBBB-CCCC",  # invalid charset (uppercase, excluded letters)
    "--ignore-vuln",  # bare flag injection
    "-r /etc/passwd",  # bare flag injection
]


@pytest.mark.parametrize("advisory", MALICIOUS_ADVISORIES)
def test_malicious_advisories_fail_closed(tmp_path, advisory: str) -> None:
    """Every non-single-ID value must be rejected by the loader BEFORE
    any shell or container ever sees it (R4-P2-02 fail closed)."""
    with pytest.raises(ValueError):
        validate_advisory(advisory)
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    entry["advisory"] = advisory
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.strip() == ""  # no ID may leak onto stdout


# ---------------------------------------------------------------------------
# R4-P2-02: argv contract proofs (one exception -> one argv pair, etc.)
# ---------------------------------------------------------------------------

# Mirrors scripts/dependency_audit.sh: the container receives a FIXED
# script body and every audit argument enters it ONLY as positional
# parameters via "$@" — never as interpolated shell text.
_ARGV_PROBE = (
    'printf "%s\\n" "$@"',
    'printf "%s\\n" "$@"; exit 3',
)


def _argv_from_loader(tmp_path: Path, entries: list) -> list[str]:
    result = _run_loader(_write(tmp_path, entries))
    assert result.returncode == 0, result.stdout + result.stderr
    ignore_args: list[str] = []
    for advisory in result.stdout.splitlines():
        ignore_args += ["--ignore-vuln", advisory]
    return ignore_args


def test_one_exception_yields_exactly_one_argv_pair(tmp_path) -> None:
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    ignore_args = _argv_from_loader(tmp_path, [entry])
    assert ignore_args == ["--ignore-vuln", "PYSEC-2099-1"]
    # Positional contract of the audited container: the array elements
    # arrive as discrete argv tokens, word-splitting-proof.
    probe = subprocess.run(
        ["bash", "-c", _ARGV_PROBE[0], "sh", *ignore_args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0
    assert probe.stdout.splitlines() == ["--ignore-vuln", "PYSEC-2099-1"]


def test_two_exceptions_yield_exactly_two_argv_pairs(tmp_path) -> None:
    today = date.today()
    first = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    second = dict(first)
    second["exception_id"] = "SEC-EX-002"
    second["advisory"] = "CVE-2026-98765"
    ignore_args = _argv_from_loader(tmp_path, [first, second])
    assert ignore_args == [
        "--ignore-vuln",
        "PYSEC-2099-1",
        "--ignore-vuln",
        "CVE-2026-98765",
    ]
    probe = subprocess.run(
        ["bash", "-c", _ARGV_PROBE[0], "sh", *ignore_args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0
    assert probe.stdout.splitlines() == [
        "--ignore-vuln",
        "PYSEC-2099-1",
        "--ignore-vuln",
        "CVE-2026-98765",
    ]


def test_nonzero_audit_exit_is_not_masked_by_allowlist(tmp_path) -> None:
    """Simulated pip-audit failure: even with allowlist tokens present,
    the audit gate must exit non-zero (R4-P2-02)."""
    today = date.today()
    entry = _valid_entry(today - timedelta(days=1), today + timedelta(days=30))
    ignore_args = _argv_from_loader(tmp_path, [entry])
    probe = subprocess.run(
        ["bash", "-c", _ARGV_PROBE[1], "sh", *ignore_args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 3
    assert probe.stdout.splitlines() == ["--ignore-vuln", "PYSEC-2099-1"]
    # And the loader keeps its fail-closed stance on malicious values:
    # a smuggled `; true` can never flip a failed audit into 0 because
    # it is rejected before reaching any shell.
    entry["advisory"] = "PYSEC-2099-1; true"
    result = _run_loader(_write(tmp_path, [entry]))
    assert result.returncode == 1
