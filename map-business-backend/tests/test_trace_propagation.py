"""P1 acceptance tests: BFF W3C propagation header forwarding.

Regression for the review finding that the BFF dropped inbound traceparent /
tracestate / baggage and fabricated a dangling parent traceparent instead.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_test_state.json")

from app.core_client import _ensure_traceparent
from app.main import app, core_client

client = TestClient(app)

VALID_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_route_forwards_inbound_propagation_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_chat_by_path(
        path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        captured["headers"] = headers
        return {"content": "ok", "meta": {}}

    monkeypatch.setattr(core_client, "chat_by_path", fake_chat_by_path)

    response = client.post(
        "/api/chat",
        json={"query": "hi"},
        headers={
            "traceparent": VALID_TRACEPARENT,
            "tracestate": "vendor=abc",
            "baggage": "user=alice",
            "X-UserId": "u1",
        },
    )

    assert response.status_code == 200
    headers = captured["headers"]
    assert headers["traceparent"] == VALID_TRACEPARENT
    assert headers["tracestate"] == "vendor=abc"
    assert headers["baggage"] == "user=alice"
    assert headers["X-UserId"] == "u1"


def test_route_without_inbound_traceparent_does_not_fabricate(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_chat_by_path(
        path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        captured["headers"] = headers
        return {"content": "ok", "meta": {}}

    monkeypatch.setattr(core_client, "chat_by_path", fake_chat_by_path)

    response = client.post("/api/chat", json={"query": "hi"})

    assert response.status_code == 200
    # map_core must mint its own root SERVER span; no dangling parent allowed
    assert "traceparent" not in {k.lower() for k in captured["headers"]}


def test_ensure_traceparent_keeps_valid_header() -> None:
    headers = {"Content-Type": "application/json", "traceparent": VALID_TRACEPARENT}
    assert _ensure_traceparent(headers)["traceparent"] == VALID_TRACEPARENT


def test_ensure_traceparent_drops_malformed_header() -> None:
    headers = {"traceparent": "not-a-traceparent"}
    assert "traceparent" not in _ensure_traceparent(headers)


def test_ensure_traceparent_absent_header_stays_absent() -> None:
    headers = {"Content-Type": "application/json"}
    assert _ensure_traceparent(headers) == headers
