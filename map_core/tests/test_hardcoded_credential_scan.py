"""P0-SEC-01 regression gate: no hardcoded credentials in the map_core tree.

Scans every ``map_core/map_core/**/*.py`` file for known hardcoded
credential patterns (gpustack tokens, OpenAI/AWS/GitHub keys, URI-embedded
credentials and ``password="<literal>"`` assignments). Any hit fails the
suite so a re-introduced secret blocks the release gate.
"""

from __future__ import annotations

import re
from pathlib import Path

# map_core/tests/test_hardcoded_credential_scan.py
#   parents[0] = map_core/tests
#   parents[1] = map_core        <- repo-side project root
MAP_CORE_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "map_core"

CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("gpustack_token", re.compile(r"gpustack_[a-z0-9]+_[a-z0-9]{16,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{24,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("slack_token", re.compile(r"xox[bap]-[A-Za-z0-9-]{10,}")),
    # URI-embedded credentials: scheme://user:password@host
    (
        "uri_embedded_password",
        re.compile(
            r"(?:mongodb|postgres(?:ql)?|mysql|redis|amqp|http)s?://"
            r"[^/\s\"']+:[^/\s\"']+@"
        ),
    ),
    # Basic-auth base64 blobs assigned as auth tokens
    (
        "basic_auth_token",
        re.compile(r"[\"']Basic\s+[A-Za-z0-9+/]{16,}={0,2}[\"']"),
    ),
    (
        "session_cookie_token",
        re.compile(r"[\"']SESSION_[A-Z0-9_]{6,}=[A-Za-z0-9_-]{16,}[\"']"),
    ),
    (
        "literal_password_assignment",
        re.compile(
            r"\bpassword\s*=\s*[\"'][^\"']{4,}[\"']",
            re.IGNORECASE,
        ),
    ),
    (
        "literal_secret_assignment",
        re.compile(
            r"\b(secret|auth_token|api_key)\s*=\s*[\"'][^\"']{12,}[\"']",
            re.IGNORECASE,
        ),
    ),
]

# Structural places where a literal may legitimately appear but must never be
# a real credential: test fixtures, redaction placeholders, env fallbacks.
_ALLOWED_LITERALS: list[str] = [
    "fake-key",
    "your_token",
    "your_user_id",
    "your_name",
    "<redacted>",
    "<model-endpoint>",
    "test-api-key",
]


def _allowed_hit(value: str) -> bool:
    return any(allowed in value for allowed in _ALLOWED_LITERALS)


def _source_files() -> list[Path]:
    return sorted(p for p in MAP_CORE_SOURCE_ROOT.rglob("*.py"))


def test_source_root_resolves_to_real_tree() -> None:
    """The gate must not be a no-op: the scanned tree has to exist."""
    assert MAP_CORE_SOURCE_ROOT.is_dir(), MAP_CORE_SOURCE_ROOT
    assert _source_files(), f"no python files under {MAP_CORE_SOURCE_ROOT}"


def test_no_hardcoded_credentials_in_map_core_source() -> None:
    violations: list[str] = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - source must be utf-8
            violations.append(f"{path}: not UTF-8")
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern_name, pattern in CREDENTIAL_PATTERNS:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if _allowed_hit(value):
                        continue
                    violations.append(
                        f"{path.relative_to(MAP_CORE_SOURCE_ROOT)}:{line_no}: "
                        f"{pattern_name}: {value[:40]}"
                    )
    assert not violations, (
        "hardcoded credentials found:\n" + "\n".join(violations)
    )
