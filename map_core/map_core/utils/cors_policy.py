"""S2-07: shared CORS policy for map_core (same contract as the BFF).

The BFF (map-business-backend/app/settings.py validate_settings) and
map_core now enforce ONE policy:

- origins and the credentials flag come from the same environment
  variables (MAP_CORS_ORIGINS, MAP_CORS_ALLOW_CREDENTIALS);
- every configured origin must be ``*`` or a well-formed
  ``http(s)://host[:port]`` - malformed entries fail at startup;
- production (ENV=prod/production) REFUSES wildcard + credentials
  (fail-closed at startup), exactly like the BFF's AC-SEC-11 guard.

Loading the policy does not require the app: uvicorn, tests and the CLI
all fail closed before any request can be served.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

DEFAULT_ORIGINS = "*"
DEFAULT_ALLOW_CREDENTIALS = "true"

# "*" or an http(s) origin with optional port - no paths, no wildcards
# inside origins, no scheme-less entries.
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?$")


@dataclass(frozen=True)
class CorsPolicy:
    origins: tuple[str, ...]
    allow_credentials: bool


def _is_production(env: str | None) -> bool:
    return (env or "").strip().lower() in {"prod", "production"}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_cors_policy(env: str | None = None) -> CorsPolicy:
    """Load and validate the shared CORS policy (fail-closed)."""
    raw_origins = os.getenv("MAP_CORS_ORIGINS", DEFAULT_ORIGINS)
    credentials = _parse_bool(
        os.getenv("MAP_CORS_ALLOW_CREDENTIALS", DEFAULT_ALLOW_CREDENTIALS)
    )
    origins = tuple(
        origin.strip() for origin in raw_origins.split(",") if origin.strip()
    )
    if not origins:
        origins = ("*",)

    for origin in origins:
        if origin == "*":
            continue
        if not _ORIGIN_RE.fullmatch(origin):
            raise RuntimeError(
                f"invalid MAP_CORS_ORIGINS entry {origin!r}: each origin must "
                "be '*' or http(s)://host[:port] (fail-closed)"
            )

    # AC-SEC-11 / S2-07: wildcard CORS combined with credentials is refused
    # in production (fail-closed at startup), same as the BFF.
    if _is_production(env) and "*" in origins and credentials:
        raise RuntimeError(
            "wildcard CORS with credentials is forbidden in production; "
            "set MAP_CORS_ORIGINS to explicit origins or "
            "MAP_CORS_ALLOW_CREDENTIALS=false (fail-closed)"
        )

    return CorsPolicy(origins=origins, allow_credentials=credentials)
