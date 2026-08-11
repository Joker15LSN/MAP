"""F-01 acceptance: create_app(overrides) works without module-level state.

The legacy tests import ``app.main.app`` directly; these tests prove the
app factory path: inject a fake store / fake core client, exercise routes,
and verify no import-time global state file is required.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_factory_test_state.json")

from app.core_client import MapCoreClient
from app.main import create_app
from app.schemas import AdminState
from app.settings import Settings


class FakeStore:
    """In-memory ConfigRepository double."""

    def __init__(self, state: AdminState | None = None) -> None:
        self._state = state or AdminState.default()

    def load(self) -> AdminState:
        return self._state

    def update(self, updater):
        result = updater(self._state)
        return self._state, result


class FakeCoreClient:
    """Minimal MapCoreClient double used by the chat routes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def chat(self, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append(("chat", payload))
        return {"content": "fake-ok", "meta": {}}

    async def chat_by_path(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        self.calls.append((path, payload))
        return {"content": "fake-ok", "meta": {}}

    async def stream_chat(self, payload: dict[str, Any], headers: dict[str, str]):
        yield b'event: done\ndata: {"content": "ok"}\n\n'

    async def stream_chat_by_path(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ):
        yield b'event: done\ndata: {"content": "ok"}\n\n'


def test_create_app_accepts_settings_override() -> None:
    settings = Settings(state_file="/tmp/does-not-need-to-exist.json")
    app = create_app(settings=settings)
    assert app.state.store is not None
    assert app.state.core_client is not None


def test_create_app_accepts_fake_store_and_client() -> None:
    store = FakeStore()
    core_client = FakeCoreClient()
    app = create_app(store=store, core_client=core_client)
    client = TestClient(app)

    summary = client.get("/api/admin/summary")
    assert summary.status_code == 200
    # AdminState.default() seeds one business agent
    assert summary.json()["business_agent_count"] == 1

    chat = client.post("/api/chat", json={"query": "hello"})
    assert chat.status_code == 200
    assert chat.json()["content"] == "fake-ok"
    assert core_client.calls[0][0] == "chat"


def test_module_level_app_uses_default_settings() -> None:
    from app.main import app, core_client, store

    assert isinstance(core_client, MapCoreClient)
    assert store is not None
    assert app.state.store is store
    assert app.state.core_client is core_client
