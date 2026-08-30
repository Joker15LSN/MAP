"""R2-P2-04: default deployment path must be provable WITHOUT env impersonation.

The second-round review rejected deploy tests that pre-set file-store env
vars before import and then called the result "default configuration". Here:

- importing ``app.main`` in a CLEAN environment (no MAP_* vars at all) must
  not touch the filesystem or the database (lazy compat singletons,
  PEP 562);
- ``load_settings()`` in a clean environment must return exactly the
  documented deployment defaults and must NOT expose any file-store field;
- app construction with explicit settings proves the OpenAPI contract
  without pretending to be the default.
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
    """Importing app.main must not construct the app (no file/DB side
    effects at import time)."""
    _clear_map_env(monkeypatch)
    if "app.main" in sys.modules:
        monkeypatch.delitem(sys.modules, "app.main", raising=False)

    module = importlib.import_module("app.main")

    assert module._lazy_app is None
    assert callable(module.create_app)


def test_load_settings_defaults_match_deployment_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_map_env(monkeypatch)
    from app.core.identity import AuthMode
    from app.settings import DEFAULT_WORKSPACE_ID, load_settings

    settings = load_settings()
    # J7b: the file-backed state store is gone — no state_file remains.
    assert not hasattr(settings, "state_file")
    assert settings.map_core_api_origin == "http://127.0.0.1:10000"
    assert settings.default_workspace_id == DEFAULT_WORKSPACE_ID
    assert settings.auth_mode == AuthMode.DEV
    assert settings.trusted_proxy_required is True  # fail-closed default


def test_boot_with_explicit_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit settings build the app without env impersonation."""
    _clear_map_env(monkeypatch)
    from app.main import create_app
    from app.settings import Settings

    app = create_app(settings=Settings())
    # starlette >= 1.6 defers router expansion until stack build, so the
    # contract is asserted through the OpenAPI document instead of app.routes.
    paths = set(app.openapi()["paths"])
    assert "/ready" in paths and "/health" in paths
