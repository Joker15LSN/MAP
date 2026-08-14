"""Shared sensitive-data redaction (P0-SEC-01, review R-07).

One sanitizer for request/response/exception payloads, API returns, logs and
OpenTelemetry attributes: sensitive keys are redacted recursively in
mappings, and text redaction covers JSON-style quoted pairs, bearer tokens,
URI userinfo passwords and sk- style keys. Tests plant a canary secret and
assert it never survives any path (see tests/test_sensitive_data.py).
"""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api-key",
    "auth_token",
    "authorization",
    "token",
    "password",
    "passwd",
    "secret",
    "access_key",
    "secret_key",
    "credential",
)

_JSON_PAIR_PATTERN = re.compile(
    r'("[^"]*(?:api[_-]?key|auth[_-]?token|token|password|passwd|secret|'
    r'access[_-]?key|secret[_-]?key|authorization)[^"]*"\s*[:=]\s*")'
    r'[^"]*(")',
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}={0,2}")
_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_URI_USERINFO_PATTERN = re.compile(r"(://[^/\s:@]+):([^/\s@]+)@")


def _normalized_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_mapping(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact values whose keys are sensitive.

    Non-mapping values are returned unchanged; the caller must not pass
    secret scalars without a key context.
    """
    if depth > 16:
        return "<redacted:max-depth>"
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _is_sensitive_key(str(key))
                else redact_mapping(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item, depth=depth + 1) for item in value]
    return value


def redact_text(text: str) -> str:
    """Redact sensitive fragments inside a plain string (logs, errors)."""
    redacted = _JSON_PAIR_PATTERN.sub(r"\1<redacted>\2", text)
    redacted = _BEARER_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = _SK_PATTERN.sub("<redacted>", redacted)
    return _URI_USERINFO_PATTERN.sub(r"\1:<redacted>@", redacted)
