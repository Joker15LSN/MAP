"""S2-07: map_core CORS - shared policy, production fail-closed, real
preflight requests.

Covers the review acceptance matrix:

- production refuses wildcard + credentials at startup (fail-closed);
- production accepts explicit origins / credentials=false;
- malformed origin entries fail at startup;
- a REAL preflight request through CORSMiddleware authorizes an allowed
  origin, and an unknown origin gets no credentials authorization.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from map_core.main import build_cors_kwargs
from map_core.utils.cors_policy import CorsPolicy, load_cors_policy


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("MAP_CORS_ORIGINS", "MAP_CORS_ALLOW_CREDENTIALS"):
        monkeypatch.delenv(key, raising=False)


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


class TestPolicy:
    def test_production_wildcard_credentials_fails(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            load_cors_policy("prod")
        assert "wildcard CORS with credentials" in str(excinfo.value)

    def test_production_explicit_origins_ok(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
        policy = load_cors_policy("prod")
        assert policy.origins == ("https://app.example.com",)
        assert policy.allow_credentials is True

    def test_production_credentials_off_ok(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_CORS_ALLOW_CREDENTIALS", "false")
        policy = load_cors_policy("prod")
        assert policy.origins == ("*",)
        assert policy.allow_credentials is False

    def test_invalid_origin_format_fails(self, monkeypatch) -> None:
        for bad in ("http://", "example.com", "https://host/path", "ftp://x.io"):
            monkeypatch.setenv("MAP_CORS_ORIGINS", bad)
            with pytest.raises(RuntimeError):
                load_cors_policy("dev")

    def test_dev_defaults_stay_wildcard(self) -> None:
        policy = load_cors_policy("dev")
        assert policy.origins == ("*",)
        assert policy.allow_credentials is True

    def test_build_cors_kwargs_fails_on_unsafe_production(
        self, monkeypatch
    ) -> None:
        # the middleware builder is what startup runs: unsafe production
        # config must fail BEFORE the app serves anything
        with pytest.raises(RuntimeError):
            build_cors_kwargs("prod")


class TestPreflight:
    def test_allowed_origin_passes_preflight(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
        client = TestClient(_policy_app(load_cors_policy("dev")))
        response = client.options(
            "/ping",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert (
            response.headers["access-control-allow-origin"]
            == "https://app.example.com"
        )
        # credentials=true must be echoed for the allowed origin
        assert response.headers["access-control-allow-credentials"] == "true"

    def test_unknown_origin_gets_no_credentials_authorization(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
        client = TestClient(_policy_app(load_cors_policy("dev")))
        response = client.options(
            "/ping",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # unknown origin: the preflight FAILS (400) and no allow-origin is
        # echoed, so the browser refuses the actual request. Starlette also
        # lists methods/credentials on the 400 response, but without an
        # allow-origin echo the preflight never authorizes anything.
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    def test_credentials_off_allows_wildcard(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_CORS_ALLOW_CREDENTIALS", "false")
        client = TestClient(_policy_app(load_cors_policy("dev")))
        response = client.get("/ping", headers={"Origin": "https://any.example.com"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers


# ---- S3-04: the frozen MAP_ENV signal ---------------------------------------


class TestMapEnvSignal:
    def test_map_env_takes_priority_over_legacy_env(self, monkeypatch) -> None:
        import os

        from map_core.main import _resolve_env

        monkeypatch.setenv("MAP_ENV", "prod")
        monkeypatch.setenv("ENV", "dev")
        assert _resolve_env() == "prod"
        # both names stay in sync for legacy readers
        assert os.environ["ENV"] == "prod"

    def test_legacy_env_is_a_fallback(self, monkeypatch) -> None:
        from map_core.main import _resolve_env

        monkeypatch.delenv("MAP_ENV", raising=False)
        monkeypatch.setenv("ENV", "pre")
        assert _resolve_env() == "pre"

    def test_production_signal_fails_unsafe_cors(self, monkeypatch) -> None:
        """MAP_ENV=prod + wildcard + credentials -> policy load fails."""
        monkeypatch.setenv("MAP_ENV", "prod")
        monkeypatch.delenv("MAP_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("MAP_CORS_ALLOW_CREDENTIALS", raising=False)
        with pytest.raises(RuntimeError, match="wildcard CORS"):
            load_cors_policy("prod")

    def test_production_signal_accepts_explicit_origins(self, monkeypatch) -> None:
        monkeypatch.setenv("MAP_ENV", "prod")
        monkeypatch.setenv("MAP_CORS_ORIGINS", "https://app.example.com")
        policy = load_cors_policy("prod")
        assert policy.origins == ("https://app.example.com",)
