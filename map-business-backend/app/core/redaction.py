"""Unified secret redaction (R1-FEEDBACK-01 / R1-AUDIT-01 / FIX-P1-FEEDBACK-01).

Free-text fields that may contain user corrections or reasons must never
persist credentials. :func:`redact_text` replaces values of sensitive keys
(``api_key``, ``token``, ``authorization``, ``cookie``, ``password``,
``secret``) in JSON-ish, ``key=value`` and ``key: value`` forms.
"""

from __future__ import annotations

import re

SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "refresh_token",
    "token",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "client_secret",
)

_KEY_RE = re.compile(r"|".join(re.escape(k) for k in SENSITIVE_KEYS), re.IGNORECASE)

# key=value / key: value / "key":"value" / 'key':'value' — value redacted.
_VALUE_RE = re.compile(
    r"(?P<prefix>[\"']?(?:" + _KEY_RE.pattern + r")[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,;}\]\"']+)",
    re.IGNORECASE,
)


def redact_text(text_value: str | None) -> str | None:
    """Redact credential-looking key/value pairs; None passes through."""
    if text_value is None:
        return None
    return _VALUE_RE.sub(lambda m: m.group("prefix") + "[REDACTED]", text_value)


def redact_payload(payload: dict | list | str | None) -> dict | list | str | None:
    """Recursively redact sensitive keys in a JSON-like structure."""
    if isinstance(payload, dict):
        return {
            (key if not re.search(_KEY_RE, key) else "[REDACTED]"): redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload
