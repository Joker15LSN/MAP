"""Application settings for the MAP business backend.

Kept deliberately small: environment-derived values that were previously
read at module import time inside ``app.main``. Tests can construct
:class:`Settings` directly or call :func:`create_app` with overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .core.identity import AuthMode
from .core.service_identity import ServiceCredential, parse_service_credentials
from .cors_policy import parse_bool

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
    auth_mode: AuthMode = field(default_factory=lambda: AuthMode(_env_or("MAP_AUTH_MODE", "dev")))
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
        default_factory=lambda: parse_bool(_env_or("MAP_TRUSTED_PROXY_REQUIRED", "true"))
    )
    # Service-to-service credentials: a token reference -> metadata
    # registry (R2-P0-02). Each entry binds one bearer token to its
    # inherent service_name/audience/scopes plus a rotation key_id; there
    # is NO shared global secret. Rotation = add a new key_id entry, then
    # revoke/remove the old one. Parsed from MAP_SERVICE_CREDENTIALS
    # (JSON array); invalid configuration fails startup (fail-closed).
    service_credentials: tuple[ServiceCredential, ...] = field(
        default_factory=lambda: parse_service_credentials(
            _env_or("MAP_SERVICE_CREDENTIALS", ""),
            default_audience=_env_or("MAP_SERVICE_AUDIENCE", "map-bff"),
        )
    )
    # Expected audience for service tokens targeting this BFF.
    service_audience: str = field(
        default_factory=lambda: _env_or("MAP_SERVICE_AUDIENCE", "map-bff")
    )
    # CORS (AC-SEC-11 / R-10): comma-separated origins. Production refuses
    # to start with a wildcard origin combined with credentials
    # (validate_settings in app/main.py fails closed).
    cors_origins: str = field(
        default_factory=lambda: _env_or("MAP_CORS_ORIGINS", "*")
    )
    cors_allow_credentials: bool = field(
        default_factory=lambda: parse_bool(_env_or("MAP_CORS_ALLOW_CREDENTIALS", "true"))
    )


def load_settings() -> Settings:
    """Load settings from the process environment (cached per call)."""
    return Settings(
        map_core_api_origin=_env_or("MAP_CORE_API_ORIGIN", "http://127.0.0.1:10000"),
        auth_mode=AuthMode(_env_or("MAP_AUTH_MODE", "dev")),
        env=_env_or("MAP_ENV", "dev").strip().lower(),
        default_workspace_id=_env_or("MAP_DEFAULT_WORKSPACE_ID", DEFAULT_WORKSPACE_ID),
        trusted_proxy_secret=_env_or("MAP_TRUSTED_PROXY_SECRET", ""),
        trusted_proxy_required=parse_bool(
            _env_or("MAP_TRUSTED_PROXY_REQUIRED", "true")
        ),
        service_credentials=parse_service_credentials(
            _env_or("MAP_SERVICE_CREDENTIALS", ""),
            default_audience=_env_or("MAP_SERVICE_AUDIENCE", "map-bff"),
        ),
        service_audience=_env_or("MAP_SERVICE_AUDIENCE", "map-bff"),
        cors_origins=_env_or("MAP_CORS_ORIGINS", "*"),
        cors_allow_credentials=parse_bool(
            _env_or("MAP_CORS_ALLOW_CREDENTIALS", "true")
        ),
    )
