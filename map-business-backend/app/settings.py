"""Application settings for the MAP business backend.

Kept deliberately small: environment-derived values that were previously
read at module import time inside ``app.main``. Tests can construct
:class:`Settings` directly or call :func:`create_app` with overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .core.identity import AuthMode

# Stable default workspace UUID shared by seed/migration/API/tests/Compose.
# Business code for this workspace stays "default" (workspaces.code).
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_WORKSPACE_CODE = "default"


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
    auth_mode: AuthMode = field(
        default_factory=lambda: AuthMode(_env_or("MAP_AUTH_MODE", "dev"))
    )
    env: str = field(default_factory=lambda: _env_or("MAP_ENV", "dev").strip().lower())
    default_workspace_id: str = field(
        default_factory=lambda: _env_or("MAP_DEFAULT_WORKSPACE_ID", DEFAULT_WORKSPACE_ID)
    )
    # trusted_header 模式下要求请求携带共享代理 secret,否则 401。
    # fail-closed: 未显式关闭验证时视为开启 (默认 true)。
    trusted_proxy_secret: str = field(
        default_factory=lambda: _env_or("MAP_TRUSTED_PROXY_SECRET", "")
    )
    trusted_proxy_required: bool = field(
        default_factory=lambda: _env_or("MAP_TRUSTED_PROXY_REQUIRED", "true").lower()
        in {"1", "true", "yes"}
    )
    # Comma-separated shared secrets for service-to-service bearer tokens
    # (rotation: all values stay valid until removed).
    service_tokens: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.strip()
            for item in _env_or("MAP_SERVICE_TOKEN_SECRET", "").split(",")
            if item.strip()
        )
    )
    # Expected audience for service tokens targeting this BFF.
    service_audience: str = field(
        default_factory=lambda: _env_or("MAP_SERVICE_AUDIENCE", "map-bff")
    )


def load_settings() -> Settings:
    """Load settings from the process environment (cached per call)."""
    return Settings(
        map_core_api_origin=_env_or("MAP_CORE_API_ORIGIN", "http://127.0.0.1:10000"),
        state_file=_env_or("MAP_BFF_STATE_FILE", "/app/data/admin_state.json"),
        auth_mode=AuthMode(_env_or("MAP_AUTH_MODE", "dev")),
        env=_env_or("MAP_ENV", "dev").strip().lower(),
        default_workspace_id=_env_or("MAP_DEFAULT_WORKSPACE_ID", DEFAULT_WORKSPACE_ID),
        trusted_proxy_secret=_env_or("MAP_TRUSTED_PROXY_SECRET", ""),
        trusted_proxy_required=_env_or("MAP_TRUSTED_PROXY_REQUIRED", "true").lower()
        in {"1", "true", "yes"},
        service_tokens=tuple(
            item.strip()
            for item in _env_or("MAP_SERVICE_TOKEN_SECRET", "").split(",")
            if item.strip()
        ),
        service_audience=_env_or("MAP_SERVICE_AUDIENCE", "map-bff"),
    )
