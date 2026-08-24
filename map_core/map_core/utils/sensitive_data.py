"""Shared sensitive-data redaction (P0-SEC-01, review R-07, S2-04).

One sanitizer for request/response/exception payloads, API returns, logs and
OpenTelemetry attributes: sensitive keys are redacted recursively in
mappings, text redaction covers JSON-style quoted pairs, bearer tokens,
URI userinfo passwords and sk- style keys.

S2-04: the sanitizer additionally receives the SET of currently resolved
secret values (e.g. the API key in use for this exact call) and wipes the
exact value from ANY string position - plain fields like answer/message that
echo a secret upstream can no longer pass through unchanged. Exact-value
wiping runs first, then the key/format rules.

Tests plant a canary secret and assert it never survives any path (see
tests/test_sensitive_data.py, tests/test_mcp_egress_guard.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

# Exact-value wiping is only applied to secrets at least this long - short
# values (e.g. "1234") would mangle ordinary text if replaced verbatim.
EXACT_WIPE_MIN_LENGTH = 8

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


def _regex_redact(text: str) -> str:
    """Key/format rules applied AFTER exact-value wiping."""
    redacted = _JSON_PAIR_PATTERN.sub(r"\1<redacted>\2", text)
    redacted = _BEARER_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = _SK_PATTERN.sub("<redacted>", redacted)
    return _URI_USERINFO_PATTERN.sub(r"\1:<redacted>@", redacted)


@dataclass(frozen=True)
class SecretRedactor:
    """S2-04: redactor with exact-value wiping plus key/format rules.

    Pass the set of secret values currently in use (API keys, tokens,
    passwords). Exact values are replaced by ``<redacted>`` at ANY string
    position before the key/format rules run, so a secret echoed back in a
    plain ``answer``/``message``/``error`` field cannot survive.
    """

    secret_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = {
            value
            for value in self.secret_values
            if isinstance(value, str) and len(value) >= EXACT_WIPE_MIN_LENGTH
        }
        # longest first so overlapping values wipe cleanly
        ordered = tuple(sorted(values, key=len, reverse=True))
        object.__setattr__(self, "secret_values", ordered)

    def redact_text(self, text: str) -> str:
        """Redact sensitive fragments inside a plain string (logs, errors)."""
        if not isinstance(text, str):
            return text
        redacted = text
        for secret in self.secret_values:
            redacted = redacted.replace(secret, "<redacted>")
        return _regex_redact(redacted)

    def redact_mapping(self, value: Any, *, depth: int = 0) -> Any:
        """Recursively redact: sensitive keys are replaced, and any string
        leaf (including plain answer/message values) goes through exact-value
        + format redaction so upstream-echoed secrets never survive."""
        if depth > 16:
            return "<redacted:max-depth>"
        if isinstance(value, dict):
            return {
                str(key): (
                    "<redacted>"
                    if _is_sensitive_key(str(key))
                    else self.redact_mapping(item, depth=depth + 1)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact_mapping(item, depth=depth + 1) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value


def make_redactor(secret_values: list[str] | tuple[str, ...] | set[str] | None) -> SecretRedactor:
    return SecretRedactor(tuple(secret_values or ()))


def redact_mapping(
    value: Any, *, secrets: list[str] | tuple[str, ...] | set[str] | None = None
) -> Any:
    """Recursively redact values whose keys are sensitive.

    ``secrets`` (optional) enables S2-04 exact-value wiping of currently
    resolved secret values at any string position.
    """
    return make_redactor(secrets).redact_mapping(value)


def redact_text(
    text: str, *, secrets: list[str] | tuple[str, ...] | set[str] | None = None
) -> str:
    """Redact sensitive fragments inside a plain string (logs, errors).

    ``secrets`` (optional) enables S2-04 exact-value wiping first.
    """
    return make_redactor(secrets).redact_text(text)
