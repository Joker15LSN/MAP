"""Machine-readable supply-chain exception loader (R3-P2-04 / R4-P2-02).

Reads ``security/dependency_exceptions.json`` and emits the advisory IDs
that ``scripts/dependency_audit.sh`` may pass as ``--ignore-vuln``. The
script is the SINGLE source of truth for allowlisting:

- every entry must carry all documented fields (advisory, owner, ticket,
  approver, approved_at, expires, reachability/mitigation rationale);
- the advisory must be a SINGLE well-formed advisory ID from an explicit
  allowlist of formats (CVE / GHSA / PYSEC). Whitespace, control
  characters, shell metacharacters and extra tokens (e.g. an embedded
  second ``--ignore-vuln``) are rejected BEFORE any shell ever sees the
  value (R4-P2-02 fail closed);
- dates are parsed with strict ISO ``date.fromisoformat``;
- an entry whose ``expires`` is on/before today FAILS the gate (exit 2) —
  an expired exception is treated exactly like a new vulnerability;
- malformed entries fail closed (exit 1);
- on success, one advisory ID per line is printed to stdout.

Usage:
    python3 scripts/load_dependency_exceptions.py [exceptions-file]

Exit codes: 0 = ok (IDs on stdout), 1 = malformed file/entry,
2 = at least one expired exception.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = REPO_ROOT / "security" / "dependency_exceptions.json"

REQUIRED_FIELDS = (
    "exception_id",
    "advisory",
    "package",
    "introduction",
    "reachability",
    "mitigation",
    "why_not_upgraded",
    "owner",
    "ticket",
    "approver",
    "approved_at",
    "expires",
)

# R4-P2-02: explicit advisory-ID formats. The character classes are
# strict subsets, so whitespace, control characters, shell metacharacters
# (`;`, `#`, `|`, `` ` ``, `$`, …), quotes and hyphen-lead injections
# (e.g. an extra ``--ignore-vuln`` token) can never match.
ADVISORY_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # CVE-2026-12345 (4+ digit years, 4+ digit sequence per CVE 2.0)
    ("CVE", re.compile(r"^CVE-\d{4}-\d{4,19}$")),
    # GHSA-xxxx-xxxx-xxxx (GitHub Security Advisory slug; GitHub's slug
    # alphabet excludes 0/1/a/e/i/l/o/s/t/u)
    ("GHSA", re.compile(r"^GHSA(?:-[23456789bcdfghjkmnpqrvwxyz]{4}){3}$")),
    # PYSEC-2026-123 (PyPA advisory database ID)
    ("PYSEC", re.compile(r"^PYSEC-\d{4}-\d{1,19}$")),
)


def validate_advisory(advisory: str) -> str:
    """Return the advisory if it is a SINGLE valid ID, else raise.

    Fail closed on anything that is not exactly one well-formed ID —
    including multi-token values that would smuggle extra pip-audit
    arguments (R4-P2-02).
    """
    if not advisory or advisory != advisory.strip():
        raise ValueError(
            f"advisory {advisory!r} has surrounding whitespace (a single "
            "well-formed advisory ID is required)"
        )
    for _name, pattern in ADVISORY_FORMATS:
        if pattern.fullmatch(advisory):
            return advisory
    raise ValueError(
        f"advisory {advisory!r} is not a single valid advisory ID; "
        f"supported formats: {', '.join(name for name, _ in ADVISORY_FORMATS)} "
        "(e.g. CVE-2026-12345, GHSA-xxxx-xxxx-xxxx, PYSEC-2026-123)"
    )


def _fail(message: str, code: int) -> None:
    print(f"[exceptions] ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _parse_iso_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        _fail(f"field '{field}' must be a strict ISO date (YYYY-MM-DD), got {value!r}", 1)
        raise AssertionError("unreachable") from None


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FILE
    if not path.is_file():
        _fail(f"exceptions file not found: {path}", 1)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"invalid JSON in {path}: {exc}", 1)
        return 1

    exceptions = payload.get("exceptions")
    if not isinstance(exceptions, list):
        _fail("'exceptions' must be a list", 1)

    today = date.today()
    seen: set[str] = set()
    expired: list[str] = []
    ids: list[str] = []

    for index, entry in enumerate(exceptions):
        label = f"exceptions[{index}]"
        if not isinstance(entry, dict):
            _fail(f"{label} must be an object", 1)
        missing = [field for field in REQUIRED_FIELDS if not str(entry.get(field, "")).strip()]
        if missing:
            _fail(f"{label} missing required fields: {', '.join(missing)}", 1)

        advisory = str(entry["advisory"])
        try:
            advisory = validate_advisory(advisory)
        except ValueError as exc:
            _fail(f"{label}: {exc}", 1)
        if advisory in seen:
            _fail(f"{label} duplicates advisory {advisory}", 1)
        seen.add(advisory)

        approved_at = _parse_iso_date(str(entry["approved_at"]).strip(), f"{label}.approved_at")
        expires = _parse_iso_date(str(entry["expires"]).strip(), f"{label}.expires")
        if approved_at > today:
            _fail(f"{label} approved_at ({approved_at}) is in the future", 1)
        if expires <= approved_at:
            _fail(f"{label} expires ({expires}) must be after approved_at ({approved_at})", 1)
        if expires <= today:
            expired.append(f"{advisory} (exception {entry['exception_id']}, expired {expires})")
            continue
        ids.append(advisory)

    if expired:
        _fail(
            "expired exceptions must be closed or re-approved; treating as "
            f"new findings: {'; '.join(expired)}",
            2,
        )

    for advisory in ids:
        print(advisory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
