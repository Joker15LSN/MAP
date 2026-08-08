"""Application settings for the MAP business backend.

Kept deliberately small: environment-derived values that were previously
read at module import time inside ``app.main``. Tests can construct
:class:`Settings` directly or call :func:`create_app` with overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_or(key: str, default: str) -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the BFF process."""

    map_core_api_origin: str = field(
        default_factory=lambda: _env_or("MAP_CORE_API_ORIGIN", "http://127.0.0.1:10000")
    )
    state_file: str = field(
        default_factory=lambda: _env_or("MAP_BFF_STATE_FILE", "/app/data/admin_state.json")
    )


def load_settings() -> Settings:
    """Load settings from the process environment (cached per call)."""
    return Settings(
        map_core_api_origin=_env_or("MAP_CORE_API_ORIGIN", "http://127.0.0.1:10000"),
        state_file=_env_or("MAP_BFF_STATE_FILE", "/app/data/admin_state.json"),
    )
