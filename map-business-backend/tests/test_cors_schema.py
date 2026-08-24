"""S4-06: shared CORS/env schema - full failure matrix + parity (BFF).

The rules live in ONE canonical file (packages/cors_policy/cors_policy.py)
vendored verbatim into the BFF as app/cors_policy.py. This suite exercises
the REAL startup path (create_app -> validate_settings) plus the strict
settings parsing, so a drift between the two services fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.identity import AuthMode
from app.cors_policy import (
    CORS_POLICY_VERSION,
    load_cors_policy,
    parse_bool,
)
from app.main import create_app
from app.settings import Settings, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "packages" / "cors_policy" / "cors_policy.py"
VENDORED = REPO_ROOT / "map-business-backend" / "app" / "cors_policy.py"

_SECRET = "fake-s3cret-value-42"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "MAP_CORS_ORIGINS",
        "MAP_CORS_ALLOW_CREDENTIALS",
        "MAP_ENV",
        "MAP_AUTH_MODE",
        "MAP_TRUSTED_PROXY_REQUIRED",
        "MAP_TRUSTED_PROXY_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def _prod_explicit(origins: str, *, credentials: bool = True) -> Settings:
    return Settings(
        auth_mode=AuthMode.TRUSTED_HEADER,
        trusted_proxy_secret=_SECRET,
        env="prod",
        state_file="/tmp/map_bff_cors_schema_state.json",
        cors_origins=origins,
        cors_allow_credentials=credentials,
    )


def test_vendored_copy_is_byte_identical_to_canonical() -> None:
    assert VENDORED.read_bytes() == CANONICAL.read_bytes(), (
        "map-business-backend/app/cors_policy.py drifted from "
        "packages/cors_policy/cors_policy.py - edit the canonical file and "
        "re-copy it to both services"
    )


def test_schema_version_is_pinned() -> None:
    assert CORS_POLICY_VERSION == "1.0.0"


# The full review matrix: every entry must fail closed identically.
_BAD_ORIGINS = [
    "",
    "https://app.example.com:0",
    "https://app.example.com:65536",
    "https://app.example.com:99999",
    "https://user@app.example.com",
    "https://user:pass@app.example.com",
    "https://app.example.com/path",
    "https://app.example.com?q=1",
    "https://app.example.com#frag",
    "https://app.example.com:abc",
    "ftp://app.example.com",
    "example.com",
    "http://",
]

_GOOD_ORIGINS = [
    "https://app.example.com",
    "https://app.example.com:443",
    "http://localhost:8000",
]

_BAD_BOOLS = ["yes", "on", "1.0", "true-ish", ""]


@pytest.mark.parametrize("bad", _BAD_ORIGINS)
def test_illegal_origin_fails_startup(bad: str) -> None:
    settings = Settings(
        auth_mode=AuthMode.DEV,
        env="dev",
        cors_origins=bad,
        cors_allow_credentials=False,
    )
    with pytest.raises(RuntimeError, match="invalid MAP_CORS_ORIGINS"):
        create_app(settings=settings, store=None, core_client=None)


@pytest.mark.parametrize("bad", _BAD_BOOLS)
def test_illegal_boolean_fails_closed(bad: str) -> None:
    with pytest.raises(RuntimeError):
        parse_bool(bad)


@pytest.mark.parametrize("bad", _BAD_BOOLS)
def test_illegal_boolean_fails_settings_load(bad: str, monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ALLOW_CREDENTIALS", bad)
    with pytest.raises(RuntimeError, match="invalid boolean"):
        load_settings()


@pytest.mark.parametrize("good", _GOOD_ORIGINS)
def test_legal_explicit_origins_start_and_pass_preflight(good: str) -> None:
    settings = _prod_explicit(good)
    app = create_app(settings=settings, store=None, core_client=None)
    client = TestClient(app)
    response = client.options(
        "/ready",
        headers={
            "Origin": good,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == good
    assert response.headers["access-control-allow-credentials"] == "true"


def test_wildcard_with_credentials_fails_startup_in_production() -> None:
    with pytest.raises(RuntimeError, match="wildcard CORS with credentials"):
        create_app(settings=_prod_explicit("*"), store=None, core_client=None)


def test_wildcard_credentials_off_is_allowed_in_production() -> None:
    settings = _prod_explicit("*", credentials=False)
    app = create_app(settings=settings, store=None, core_client=None)
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "https://any.example.com"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_unknown_env_fails_startup() -> None:
    settings = Settings(
        auth_mode=AuthMode.DEV,
        env="staging",
        cors_origins="https://app.example.com",
        cors_allow_credentials=False,
    )
    with pytest.raises(RuntimeError, match="unknown MAP_ENV"):
        create_app(settings=settings, store=None, core_client=None)


def test_unknown_origin_gets_no_cors_authorization() -> None:
    app = create_app(
        settings=_prod_explicit("https://app.example.com"),
        store=None,
        core_client=None,
    )
    client = TestClient(app)
    response = client.options(
        "/ready",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_bff_load_cors_policy_matches_map_core_contract(monkeypatch) -> None:
    # The vendored module is the same code: exercise it directly too.
    monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
    policy = load_cors_policy("prod")
    assert policy.origins == ("https://app.example.com",)
    assert policy.allow_credentials is True
