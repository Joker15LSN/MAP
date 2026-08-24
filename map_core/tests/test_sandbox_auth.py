"""S7-04 acceptance: credential temporal validity + explicit audience.

The sixth round accepted missing/forged/wrong-audience/wrong-scope, but
the credential model had no expiry at all and silently inherited the
deployment audience default when a credential omitted ``audience``. This
module pins the seventh-round fixes:

- every credential must explicitly carry a non-empty ``audience`` and an
  ``expires_at``; missing either is a configuration error (ValueError),
  never a wider grant;
- ``not_before`` is optional but honored when present;
- an expired / not-yet-valid token authenticates to ``(None, "missing")``
  so the router returns HTTP 401 BEFORE ledger/OpenSandbox;
- the Bearer scheme match is case-insensitive (RFC 7235).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from map_core.service.sandbox_auth import (
    authenticate_sandbox_request,
    parse_sandbox_credentials,
)

TOKEN = "svc-token"
NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def _credential(**overrides) -> dict:
    entry = {
        "key_id": "k-1",
        "token": TOKEN,
        "service_name": "map-worker",
        "audience": "map-core",
        "scopes": ["sandbox:execute"],
        "expires_at": "2026-08-16T12:00:00Z",
    }
    entry.update(overrides)
    return entry


def _auth(scheme: str = "Bearer") -> str:
    return f"{scheme} {TOKEN}"


def test_valid_credential_in_window_authenticates(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    credentials = parse_sandbox_credentials(json.dumps([_credential()]))
    credential, reason = authenticate_sandbox_request(_auth(), credentials, now=NOW)
    assert credential is not None
    assert reason is None
    assert credential.key_id == "k-1"


def test_expired_credential_is_missing_not_forbidden(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    credentials = parse_sandbox_credentials(
        json.dumps([_credential(expires_at="2026-08-15T11:59:59Z")])
    )
    credential, reason = authenticate_sandbox_request(_auth(), credentials, now=NOW)
    assert credential is None
    assert reason == "missing"  # HTTP 401, never a widened grant


def test_not_before_in_the_future_is_missing(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    credentials = parse_sandbox_credentials(
        json.dumps([_credential(not_before="2026-08-15T12:00:01Z")])
    )
    credential, reason = authenticate_sandbox_request(_auth(), credentials, now=NOW)
    assert credential is None
    assert reason == "missing"


def test_not_before_in_the_past_is_valid(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    credentials = parse_sandbox_credentials(
        json.dumps([_credential(not_before="2026-08-15T11:59:59Z")])
    )
    credential, reason = authenticate_sandbox_request(_auth(), credentials, now=NOW)
    assert credential is not None and reason is None


def test_missing_expires_at_is_configuration_error() -> None:
    entry = _credential()
    del entry["expires_at"]
    with pytest.raises(ValueError, match="expires_at"):
        parse_sandbox_credentials(json.dumps([entry]))


def test_missing_audience_is_configuration_error(monkeypatch) -> None:
    # Even when the deployment audience default is configured, a
    # credential that omits audience must NOT inherit it silently.
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    entry = _credential()
    del entry["audience"]
    with pytest.raises(ValueError, match="audience"):
        parse_sandbox_credentials(json.dumps([entry]))


def test_empty_audience_is_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    with pytest.raises(ValueError, match="audience"):
        parse_sandbox_credentials(json.dumps([_credential(audience=" ")]))


def test_malformed_expiry_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="expires_at"):
        parse_sandbox_credentials(json.dumps([_credential(expires_at="tomorrow")]))


def test_naive_expiry_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_sandbox_credentials(
            json.dumps([_credential(expires_at="2026-08-16T12:00:00")])
        )


def test_bearer_scheme_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    credentials = parse_sandbox_credentials(json.dumps([_credential()]))
    credential, reason = authenticate_sandbox_request(
        _auth(scheme="bEaReR"), credentials, now=NOW
    )
    assert credential is not None and reason is None


def test_wrong_audience_still_forbidden_for_valid_token(monkeypatch) -> None:
    monkeypatch.setenv("MAP_SANDBOX_SERVICE_AUDIENCE", "map-core")
    credentials = parse_sandbox_credentials(
        json.dumps([_credential(audience="other-service")])
    )
    credential, reason = authenticate_sandbox_request(_auth(), credentials, now=NOW)
    assert credential is None
    assert reason == "forbidden"