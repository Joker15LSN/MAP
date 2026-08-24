"""S4-06: shared CORS/env schema - full failure matrix + parity.

The rules live in ONE canonical file (packages/cors_policy/cors_policy.py)
vendored verbatim into map_core. This suite pins the contract both ways:

- parity: the vendored copy is byte-identical to the canonical copy and the
  version constant matches;
- the full review matrix fails closed identically for every illegal input.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from map_core.utils.cors_policy import (
    CORS_POLICY_VERSION,
    CorsPolicy,
    load_cors_policy,
    parse_bool,
    validate_origin,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "packages" / "cors_policy" / "cors_policy.py"
VENDORED = REPO_ROOT / "map_core" / "map_core" / "utils" / "cors_policy.py"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("MAP_CORS_ORIGINS", "MAP_CORS_ALLOW_CREDENTIALS"):
        monkeypatch.delenv(key, raising=False)


def test_vendored_copy_is_byte_identical_to_canonical() -> None:
    assert VENDORED.read_bytes() == CANONICAL.read_bytes(), (
        "map_core/utils/cors_policy.py drifted from packages/cors_policy/"
        "cors_policy.py - edit the canonical file and re-copy it to both services"
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
def test_illegal_origin_fails_closed(bad: str, monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", bad)
    with pytest.raises(RuntimeError):
        load_cors_policy("dev")


@pytest.mark.parametrize("bad", _BAD_BOOLS)
def test_illegal_boolean_fails_closed(bad: str, monkeypatch) -> None:
    with pytest.raises(RuntimeError):
        parse_bool(bad)
    monkeypatch.setenv("MAP_CORS_ALLOW_CREDENTIALS", bad)
    with pytest.raises(RuntimeError):
        load_cors_policy("dev")


@pytest.mark.parametrize("good", _GOOD_ORIGINS)
def test_legal_explicit_origins_are_accepted(good: str, monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", good)
    policy = load_cors_policy("dev")
    assert policy.origins == (good,)


def test_wildcard_with_credentials_fails_in_production(monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", "*")
    monkeypatch.setenv("MAP_CORS_ALLOW_CREDENTIALS", "true")
    with pytest.raises(RuntimeError, match="wildcard CORS with credentials"):
        load_cors_policy("prod")


def test_wildcard_credentials_off_is_allowed_in_production(monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", "*")
    monkeypatch.setenv("MAP_CORS_ALLOW_CREDENTIALS", "false")
    policy = load_cors_policy("prod")
    assert policy.origins == ("*",)
    assert policy.allow_credentials is False


def test_unknown_env_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
    with pytest.raises(RuntimeError, match="unknown MAP_ENV"):
        load_cors_policy("staging")


def _policy_app(policy: CorsPolicy) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(policy.origins),
        allow_credentials=policy.allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    return app


def test_legal_origin_passes_real_preflight(monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
    policy = load_cors_policy("prod")
    client = TestClient(_policy_app(policy))
    response = client.options(
        "/ping",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unknown_origin_gets_no_cors_authorization(monkeypatch) -> None:
    monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
    policy = load_cors_policy("dev")
    client = TestClient(_policy_app(policy))
    response = client.options(
        "/ping",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_validate_origin_accepts_wildcard() -> None:
    validate_origin("*")
