"""OpenAPI contract snapshot (FIX-P2-CONTRACT-E2E-01 / R2-P2-04).

- the FULL normalized schema (paths, request/response schemas, required,
  types, status codes) must match the committed snapshot — not just the
  (method, path) set; drift is only allowed through the explicit-review
  allowlist (openapi_change_allowlist.json, entries expire);
- every path/verb in the committed snapshot must still exist (no deletions,
  no field/type drift on legacy chat/admin routes);
- new /api/v1 and /internal/v1 errors use the standard envelope at runtime
  (FastAPI's default {"detail": ...} never leaks as a product error);
- regenerate the snapshot intentionally with:
    uv run python tests/contracts/gen_snapshot.py
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_contract_test_state.json")

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.settings import Settings

SNAPSHOT_PATH = Path(__file__).parent / "openapi_snapshot.json"
ALLOWLIST_PATH = Path(__file__).parent / "openapi_change_allowlist.json"

LEGACY_CHAT_PATHS = {
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream/v2"),
    ("POST", "/api/chat/flow/v1"),
    ("POST", "/api/chat/stream/flow/v1"),
}

LEGACY_ADMIN_PATHS = {
    ("GET", "/api/admin/full-config"),
    ("GET", "/api/admin/summary"),
    ("PUT", "/api/admin/model-center"),
    ("GET", "/api/admin/audit-logs"),
    ("PUT", "/api/admin/master-agent"),
}

NEW_API_PATHS = {
    ("POST", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations/{conversation_id}"),
    ("POST", "/api/v1/conversations/{conversation_id}/messages:stream"),
    ("POST", "/api/v1/messages/{message_id}:stop"),
    ("PUT", "/api/v1/messages/{message_id}/feedback"),
    ("DELETE", "/api/v1/messages/{message_id}/feedback"),
    ("GET", "/api/v1/admin/feedback"),
    ("GET", "/api/v1/admin/audit-events"),
    ("GET", "/api/v1/admin/audit-events/verify"),
    ("GET", "/internal/v1/ping"),
    ("GET", "/internal/v1/runtime-config-snapshots/{snapshot_id}"),
    ("GET", "/ready"),
    ("GET", "/health"),
}


def _all_operations(openapi: dict) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for path, methods in openapi["paths"].items():
        for method in ("get", "put", "post", "delete", "patch"):
            if method in methods:
                result.add((method.upper(), path))
    return result


def _app():
    return create_app(
        settings=Settings(auth_mode="dev", state_file="/tmp/map_bff_contract_test_state.json")
    )


def test_snapshot_has_no_deletions_and_no_unexpected_additions() -> None:
    """The committed snapshot is the contract: no silent deletions/additions."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = _all_operations(_app().openapi())
    snapshot_ops = _all_operations(snapshot)

    deleted = snapshot_ops - current
    assert not deleted, f"paths removed from OpenAPI contract: {sorted(deleted)}"

    added = current - snapshot_ops
    assert not added, (
        f"new OpenAPI operations must update the snapshot intentionally: {sorted(added)}"
    )


# --- R2-P2-04: FULL schema diff, not just (method, path) sets ----------------


def _iter_diff_paths(expected, actual, prefix: str = ""):
    """Yield dotted paths where the two OpenAPI documents differ."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in expected or key not in actual:
                yield path
            else:
                yield from _iter_diff_paths(expected[key], actual[key], path)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            yield prefix or "<root>"
            return
        for index, (exp_item, act_item) in enumerate(zip(expected, actual, strict=True)):
            yield from _iter_diff_paths(exp_item, act_item, f"{prefix}[{index}]")
        return
    if expected != actual or type(expected) is not type(actual):
        yield prefix or "<root>"


def _parse_iso_date(value: object, field: str, entry: dict) -> datetime.date:
    """Strict ISO-8601 date parsing: anything else is a broken approval."""
    if not isinstance(value, str):
        raise AssertionError(f"allowlist entry '{field}' must be a YYYY-MM-DD string: {entry!r}")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise AssertionError(
            f"allowlist entry '{field}' is not a strict ISO date: {entry!r}"
        ) from exc


def _load_allowlist() -> list[dict]:
    """Parse and validate the allowlist UNCONDITIONALLY (R3-P2-02).

    Deterministic behaviours:
    - empty entries       -> the schema must match exactly;
    - expired entry       -> fail, even on a zero-diff run;
    - missing field       -> fail;
    - non-ISO date        -> fail;
    - approved_at > today or > expires -> fail (backdated/future approval);
    - too-broad prefix    -> fail (must target at least one dotted segment,
                             e.g. ``paths.<route>``, never the whole document);
    - unmatched entry     -> tolerated no-op, still fully validated (it may
                             legitimately outlive the drift it approved).
    """
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    today = datetime.date.today()
    for entry in entries:
        for field in ("path_prefix", "reason", "approved_by", "approved_at", "expires"):
            assert entry.get(field), f"allowlist entry missing '{field}': {entry!r}"
        approved_at = _parse_iso_date(entry["approved_at"], "approved_at", entry)
        expires = _parse_iso_date(entry["expires"], "expires", entry)
        assert expires >= today, (
            f"expired OpenAPI allowlist entry: {entry!r} — remove it "
            "and regenerate the snapshot, or re-approve with a new expiry"
        )
        assert approved_at <= today, f"allowlist approved in the future: {entry!r}"
        assert approved_at <= expires, f"allowlist approved after expiry: {entry!r}"
        prefix = str(entry["path_prefix"])
        assert "." in prefix, (
            f"allowlist path_prefix too broad (must target a dotted OpenAPI "
            f"sub-path, e.g. 'paths.<route>'): {entry!r}"
        )
    return entries


def test_openapi_full_schema_matches_snapshot() -> None:
    """Deep schema contract: request/response schemas, required, types,
    status codes — any drift fails unless explicitly reviewed (allowlist).

    R3-P2-02: the allowlist is validated UNCONDITIONALLY — an expired entry
    must fail even on a zero-diff run, otherwise stale approvals survive
    forever as long as the schema happens to match.
    """
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = _app().openapi()

    entries = _load_allowlist()
    diffs = sorted(set(_iter_diff_paths(snapshot, current)))
    if not diffs:
        return
    rejected = [
        path
        for path in diffs
        if not any(path.startswith(e["path_prefix"]) for e in entries)
    ]
    assert not rejected, (
        "OpenAPI schema drifted from the committed snapshot (full-schema "
        f"compare, {len(diffs)} difference(s)). Review and either revert or "
        "regenerate intentionally: uv run python tests/contracts/gen_snapshot.py\n"
        f"first differences: {rejected[:20]}"
    )


# --- R3-P2-02: allowlist validation is unconditional ------------------------


def _write_allowlist(tmp_path, entries: list[dict]):
    path = tmp_path / "openapi_change_allowlist.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def _valid_entry(**overrides) -> dict:
    entry = {
        "path_prefix": "paths./api/v1/admin/audit-events",
        "reason": "reviewed schema change",
        "approved_by": "alice",
        "approved_at": "2026-08-01",
        "expires": "2999-01-01",
    }
    entry.update(overrides)
    return entry


def test_expired_allowlist_fails_even_without_schema_diff(tmp_path, monkeypatch) -> None:
    """The R3-P2-02 reproduction: zero schema diff must NOT skip the
    allowlist — an expired entry still fails the contract."""
    import test_openapi_contract as module

    monkeypatch.setattr(
        module,
        "ALLOWLIST_PATH",
        _write_allowlist(tmp_path, [_valid_entry(expires="2020-01-01", approved_at="2019-01-01")]),
    )
    with pytest.raises(AssertionError, match="expired"):
        test_openapi_full_schema_matches_snapshot()


@pytest.mark.parametrize(
    "entry",
    [
        _valid_entry(expires=""),  # missing expiry
        _valid_entry(reason=""),  # missing reason
        _valid_entry(expires="01/01/2999"),  # non-ISO date
        _valid_entry(approved_at="2999-01-02"),  # approved in the future
        _valid_entry(approved_at="2999-01-02", expires="2999-01-01"),  # after expiry
        _valid_entry(path_prefix="paths"),  # too broad: whole paths tree
        _valid_entry(path_prefix=""),  # broadest possible
    ],
)
def test_allowlist_entry_validation_is_deterministic(tmp_path, monkeypatch, entry) -> None:
    import test_openapi_contract as module

    monkeypatch.setattr(module, "ALLOWLIST_PATH", _write_allowlist(tmp_path, [entry]))
    with pytest.raises(AssertionError):
        module._load_allowlist()


def test_empty_allowlist_requires_exact_match(tmp_path, monkeypatch) -> None:
    import test_openapi_contract as module

    monkeypatch.setattr(module, "ALLOWLIST_PATH", _write_allowlist(tmp_path, []))
    assert module._load_allowlist() == []
    # The committed snapshot currently matches exactly, so the full test
    # passes with an empty (but validated) allowlist.
    test_openapi_full_schema_matches_snapshot()


def test_legacy_chat_and_admin_routes_are_preserved() -> None:
    current = _all_operations(_app().openapi())
    assert current >= LEGACY_CHAT_PATHS
    assert current >= LEGACY_ADMIN_PATHS


def test_new_api_routes_are_present() -> None:
    current = _all_operations(_app().openapi())
    assert current >= NEW_API_PATHS


async def test_v1_errors_use_standard_envelope_not_fastapi_detail(_engine) -> None:
    """Runtime check: /api/v1 4xx bodies are {code,message,details,request_id}.

    This test exercises real /api/v1 handlers against PostgreSQL. Pytest
    collects ``tests/contracts/`` before ``tests/integration/``, so it must
    not rely on an earlier test having run Alembic — requesting the shared
    ``_engine`` fixture runs the idempotent ``upgrade head`` explicitly and
    makes a fresh database reproducible (Step 0 baseline gate).
    """
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 404 conversation
        response = await client.get(f"/api/v1/conversations/{uuid.uuid4()}")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"code", "message", "details", "request_id"}
        assert "detail" not in body
        assert body["code"] == "RESOURCE_NOT_FOUND"
        assert body["request_id"]

        # 409 idempotency conflict on a real conversation
        created = await client.post(
            "/api/v1/conversations",
            json={"mode": "global"},
            headers={"Idempotency-Key": "e2e-conflict-key"},
        )
        assert created.status_code == 201
        conflict = await client.post(
            "/api/v1/conversations",
            json={"mode": "flow"},
            headers={"Idempotency-Key": "e2e-conflict-key"},
        )
        assert conflict.status_code == 409
        conflict_body = conflict.json()
        assert conflict_body["code"] == "IDEMPOTENCY_CONFLICT"
        assert set(conflict_body) == {"code", "message", "details", "request_id"}

        # 422 validation envelope (bad rating)
        response = await client.put(
            "/api/v1/messages/00000000-0000-0000-0000-000000000001/feedback",
            json={"rating": "great"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"

        # internal service identity: browser token rejected with envelope
        internal = await client.get(
            "/internal/v1/ping",
            headers={
                "Authorization": "Bearer browser-token",
                "X-Service-Name": "browser",
                "X-Service-Audience": "map-bff",
                "X-Service-Scopes": "internal.ping",
            },
        )
        assert internal.status_code == 401
        assert internal.json()["code"] == "INVALID_SERVICE_IDENTITY"


async def test_legacy_error_shape_kept() -> None:
    """Legacy /api/* keeps FastAPI's {"detail": ...} shape (compat)."""
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/admin/nonexistent-route")
        assert response.status_code == 404
        body = response.json()
        assert "detail" in body and "code" not in body
