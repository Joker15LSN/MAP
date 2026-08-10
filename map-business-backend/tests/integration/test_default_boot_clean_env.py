"""R2-P2-04: default deployment path must be provable WITHOUT env impersonation.

The second-round review rejected deploy tests that pre-set
``MAP_BFF_STATE_FILE`` before import and then called the result "default
configuration". Here:

- importing ``app.main`` in a CLEAN environment (no MAP_* vars at all) must
  not touch the filesystem (lazy compat singletons, PEP 562);
- ``load_settings()`` in a clean environment must return exactly the
  documented deployment defaults;
- when a test needs a writable state file, it says so explicitly through
  test settings instead of pretending to be the default.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


def _clear_map_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("MAP_"):
            monkeypatch.delenv(key, raising=False)


def test_import_app_main_with_clean_env_has_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repro: before R2-P2-04 this raised OSError ('/app' read-only) because
    app.main built the app eagerly at import time."""
    _clear_map_env(monkeypatch)
    if "app.main" in sys.modules:
        monkeypatch.delitem(sys.modules, "app.main", raising=False)

    module = importlib.import_module("app.main")

    # Import alone must not construct the app (no /app/data mkdir).
    assert module._lazy_app is None
    assert callable(module.create_app)


def test_load_settings_defaults_match_deployment_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_map_env(monkeypatch)
    from app.core.identity import AuthMode
    from app.settings import DEFAULT_WORKSPACE_ID, load_settings

    settings = load_settings()
    assert settings.state_file == "/app/data/admin_state.json"
    assert settings.map_core_api_origin == "http://127.0.0.1:10000"
    assert settings.default_workspace_id == DEFAULT_WORKSPACE_ID
    assert settings.auth_mode == AuthMode.DEV
    assert settings.trusted_proxy_required is True  # fail-closed default


@pytest.mark.asyncio
async def test_boot_with_explicit_test_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Writable paths come from EXPLICIT test settings, not env defaults."""
    _clear_map_env(monkeypatch)
    from app.main import create_app
    from app.settings import Settings

    state_file = tmp_path / "explicit_state.json"
    app = create_app(settings=Settings(state_file=str(state_file)))
    # starlette >= 1.6 defers router expansion until stack build, so the
    # contract is asserted through the OpenAPI document instead of app.routes.
    paths = set(app.openapi()["paths"])
    assert "/ready" in paths and "/health" in paths
