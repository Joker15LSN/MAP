"""Shared, versioned CORS + environment schema (S4-06).

Single source of truth for the MAP deployment CORS policy and the strict
MAP_ENV enum. This file is the canonical copy; it is vendored VERBATIM
into both services so the two separate repos can never drift apart:

- map_core/map_core/utils/cors_policy.py
- map-business-backend/app/cors_policy.py

Every copy must stay byte-identical to packages/cors_policy/cors_policy.py.
Each service test suite asserts that parity (and pins CORS_POLICY_VERSION)
so a one-sided edit fails CI.

Rules enforced here (fail-closed, before any request is served):

- an origin is the literal "*" or an "http(s)://host[:port]" URL with a
  real hostname, an optional port in 1..65535, and NO userinfo, path, query
  or fragment (validated with urllib.parse.urlsplit, not a regex);
- booleans come from a strict enum ("true"/"false"/"1"/"0") and any other
  value raises instead of silently coercing;
- MAP_ENV is one of "dev"/"test"/"pre"/"prod"; anything else raises instead
  of silently falling back to "dev";
- production (MAP_ENV=prod) refuses wildcard origin + credentials.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Bump whenever the rules change; parity tests pin this exact value.
CORS_POLICY_VERSION = "1.0.0"

DEFAULT_ORIGINS = "*"
DEFAULT_ALLOW_CREDENTIALS = "true"

# S3-04: the only deployment environments. Unknown values fail closed.
ENV_DEV = "dev"
ENV_TEST = "test"
ENV_PRE = "pre"
ENV_PROD = "prod"
KNOWN_ENVIRONMENTS = frozenset({ENV_DEV, ENV_TEST, ENV_PRE, ENV_PROD})

# Strict boolean enum: exactly these literals, nothing else.
_TRUE_VALUES = frozenset({"true", "1"})
_FALSE_VALUES = frozenset({"false", "0"})

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# RFC 1035 hostname characters (labels validated separately below).
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")


@dataclass(frozen=True)
class CorsPolicy:
    origins: tuple[str, ...]
    allow_credentials: bool
    schema_version: str = CORS_POLICY_VERSION


def normalize_env(value: str | None) -> str:
    """Return the canonical env name or raise (fail-closed)."""
    env = (value or "").strip().lower()
    if env not in KNOWN_ENVIRONMENTS:
        raise RuntimeError(
            f"unknown MAP_ENV {value!r}: expected one of "
            f"{sorted(KNOWN_ENVIRONMENTS)} (fail-closed)"
        )
    return env


def is_production(env: str | None) -> bool:
    """True only for the canonical production environment name."""
    if env is None:
        return False
    return normalize_env(env) == ENV_PROD


def parse_bool(value: str) -> bool:
    """Strictly parse a boolean from its enumerated literals.

    Accepts "true"/"false" and "1"/"0" (case-insensitive, surrounding
    whitespace tolerated). Anything else raises rather than coercing.
    """
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError(
        f"invalid boolean value {value!r}: expected one of "
        f"{sorted(_TRUE_VALUES | _FALSE_VALUES)} (fail-closed)"
    )


def _valid_hostname(hostname: str) -> bool:
    if not hostname or not _HOSTNAME_RE.fullmatch(hostname):
        return False
    if hostname.startswith(("-", ".")) or hostname.endswith(("-", ".")):
        return False
    return ".." not in hostname


def validate_origin(origin: str) -> None:
    """Validate ONE configured origin (fail-closed)."""
    if origin == "*":
        return
    if not origin:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "each origin must be '*' or http(s)://host[:port] (fail-closed)"
        )

    try:
        parts = urlsplit(origin)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: {exc} (fail-closed)"
        ) from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "scheme must be http or https (fail-closed)"
        )
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "userinfo is not allowed (fail-closed)"
        )
    hostname = parts.hostname or ""
    if not _valid_hostname(hostname):
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "hostname is missing or malformed (fail-closed)"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "port must be an integer in 1..65535 (fail-closed)"
        ) from exc
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "port must be in 1..65535 (fail-closed)"
        )
    if parts.path:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "path is not allowed (fail-closed)"
        )
    if parts.query or parts.fragment:
        raise RuntimeError(
            f"invalid MAP_CORS_ORIGINS entry {origin!r}: "
            "query and fragment are not allowed (fail-closed)"
        )


def parse_origins(raw: str) -> tuple[str, ...]:
    """Split and validate the comma-separated origin list (fail-closed)."""
    origins = tuple(entry.strip() for entry in raw.split(","))
    if not origins:
        raise RuntimeError("MAP_CORS_ORIGINS must not be empty (fail-closed)")
    for origin in origins:
        validate_origin(origin)
    return origins


def load_cors_policy(env: str | None = None) -> CorsPolicy:
    """Load and validate the shared CORS policy from the environment."""
    raw_origins = os.getenv("MAP_CORS_ORIGINS", DEFAULT_ORIGINS)
    raw_credentials = os.getenv(
        "MAP_CORS_ALLOW_CREDENTIALS", DEFAULT_ALLOW_CREDENTIALS
    )
    credentials = parse_bool(raw_credentials)
    origins = parse_origins(raw_origins)

    if is_production(env) and "*" in origins and credentials:
        raise RuntimeError(
            "wildcard CORS with credentials is forbidden in production; "
            "set MAP_CORS_ORIGINS to explicit origins or "
            "MAP_CORS_ALLOW_CREDENTIALS=false (fail-closed)"
        )
    return CorsPolicy(origins=origins, allow_credentials=credentials)
