import os
from collections.abc import AsyncGenerator
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_test_state.json")

from app.main import app, core_client


client = TestClient(app)


def test_chat_flow_v1_forwards_to_flow_domain(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_chat_by_path(path: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
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


def test_chat_stream_flow_v1_forwards_to_flow_domain(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_stream_chat_by_path(
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncGenerator[bytes, None]:
        captured["path"] = path
        captured["payload"] = payload
        captured["headers"] = headers
        yield b"event: done\ndata: {\"content\": \"ok\"}\n\n"

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
