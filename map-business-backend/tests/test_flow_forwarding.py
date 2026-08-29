import os
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_test_state.json")

from app.api.deps import get_runtime_snapshots
from app.main import app, core_client

client = TestClient(app)

SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SNAPSHOT_DIGEST = "d" * 64


class _FakeSnapshots:
    async def get_current(self) -> SimpleNamespace:
        return SimpleNamespace(id=SNAPSHOT_ID, digest=SNAPSHOT_DIGEST)


def _install_snapshot_override(monkeypatch) -> None:
    monkeypatch.setitem(
        app.dependency_overrides,
        get_runtime_snapshots,
        lambda: _FakeSnapshots(),
    )


def test_chat_flow_v1_forwards_to_flow_domain(monkeypatch) -> None:
    _install_snapshot_override(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_chat_by_path(
        path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        captured["path"] = path
        captured["payload"] = payload
        captured["headers"] = headers
        return {"content": "ok", "meta": {"mode": "flow"}}

    monkeypatch.setattr(core_client, "chat_by_path", fake_chat_by_path)

    response = client.post(
        "/api/chat/flow/v1",
        json={
            "query": "订单确认收入",
            "flow_config": {"scenario_policy": {"enabled": True}},
            "tool_context": {"scenario": {"matched_scenarios": ["order_revenue_confirmation"]}},
        },
        headers={"X-request-token": "token-1", "X-UserId": "u1", "X-UserName": "n1"},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "ok"
    assert captured["path"] == "/flow_domain/chat/v1"
    assert captured["payload"]["flow_config"]["scenario_policy"]["enabled"] is True
    assert captured["headers"]["X-request-token"] == "token-1"
    assert captured["headers"]["X-Runtime-Snapshot-ID"] == str(SNAPSHOT_ID)
    assert captured["headers"]["X-Runtime-Snapshot-Digest"] == SNAPSHOT_DIGEST


def test_chat_stream_flow_v1_forwards_to_flow_domain(monkeypatch) -> None:
    _install_snapshot_override(monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_stream_chat_by_path(
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncGenerator[bytes, None]:
        captured["path"] = path
        captured["payload"] = payload
        captured["headers"] = headers
        yield b'event: done\ndata: {"content": "ok"}\n\n'

    monkeypatch.setattr(core_client, "stream_chat_by_path", fake_stream_chat_by_path)

    response = client.post(
        "/api/chat/stream/flow/v1",
        json={"query": "跨业务域串行查询"},
        headers={"X-request-token": "token-2"},
    )

    assert response.status_code == 200
    assert "event: done" in response.text
    assert captured["path"] == "/flow_domain/chat/stream/v1"
    assert captured["payload"]["query"] == "跨业务域串行查询"
    assert captured["headers"]["X-request-token"] == "token-2"
    assert captured["headers"]["X-Runtime-Snapshot-ID"] == str(SNAPSHOT_ID)
    assert captured["headers"]["X-Runtime-Snapshot-Digest"] == SNAPSHOT_DIGEST


def test_chat_flow_v1_returns_503_when_no_current_snapshot(monkeypatch) -> None:
    class EmptySnapshots:
        async def get_current(self) -> None:
            return None

    monkeypatch.setitem(
        app.dependency_overrides,
        get_runtime_snapshots,
        lambda: EmptySnapshots(),
    )

    response = client.post(
        "/api/chat/flow/v1",
        json={"query": "订单确认收入"},
        headers={"X-request-token": "token-3"},
    )

    assert response.status_code == 503
    assert response.headers["X-MAP-Error-Code"] == "RUNTIME_SNAPSHOT_UNAVAILABLE"
