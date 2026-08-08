"""R1-CONV-01 acceptance: conversation persistence + streaming checkpoint.

Uses the real PostgreSQL container (tests/integration fixtures) and a fake
core client emitting SSE content_delta frames. Requests run through httpx
ASGITransport on the same event loop as the DB session.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_conv_test_state.json")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.asyncio

WORKSPACE = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


class FakeStreamCoreClient:
    """Core client double: yields SSE chunks and records forwarded headers."""

    def __init__(self, chunks: list[bytes] | None = None, fail: bool = False) -> None:
        self.chunks = chunks or [
            'event: start\ndata: {"request_id":"r1"}\n\n'.encode("utf-8"),
            'event: content_delta\ndata: {"content":"你"}\n\n'.encode("utf-8"),
            'event: content_delta\ndata: {"content":"好"}\n\n'.encode("utf-8"),
            'event: done\ndata: {"content":"你好","task_id":"t-1"}\n\n'.encode("utf-8"),
        ]
        self.fail = fail
        self.forwarded: dict[str, str] = {}

    async def stream_chat(self, payload, headers):
        self.forwarded = headers
        if self.fail:
            raise RuntimeError("core down")
        for chunk in self.chunks:
            yield chunk

    async def stream_chat_by_path(self, path, payload, headers):
        self.forwarded = headers
        if self.fail:
            raise RuntimeError("core down")
        for chunk in self.chunks:
            yield chunk


@pytest_asyncio.fixture
async def app_and_core(_engine, session):
    core = FakeStreamCoreClient()
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file="/tmp/map_bff_conv_test_state.json",
            default_workspace_id=WORKSPACE,
        ),
        store=None,
        core_client=core,
    )

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override
    return app, core


async def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    )


def _parse_sse(body: bytes) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in body.decode("utf-8").split("\n\n"):
        data_lines: list[str] = []
        event = "message"
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if frame.strip():
            data = json.loads("\n".join(data_lines) or "{}")
            events.append((event, data))
    return events


async def test_create_and_restore_conversation(app_and_core, session) -> None:
    app, _ = app_and_core
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/conversations", json={"mode": "global", "title": "测试会话"}
        )
        assert response.status_code == 201
        conversation_id = response.json()["id"]

        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert detail.json()["title"] == "测试会话"
        assert detail.json()["messages"] == []

        listed = await client.get("/api/v1/conversations")
        assert listed.status_code == 200
        assert any(item["id"] == conversation_id for item in listed.json())


async def test_stream_persists_user_and_assistant_pair(app_and_core, session) -> None:
    app, core = app_and_core
    async with await _client(app) as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]

        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "你好", "request_id": "req-1"},
        )
        assert response.status_code == 200
        events = _parse_sse(response.content)
        event_names = [event for event, _ in events]
        assert "message.started" in event_names
        assert "content_delta" in event_names
        assert event_names[-1] == "done"

        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        messages = detail["messages"]
        assert len(messages) == 2
        user_message, assistant_message = messages
        assert user_message["role"] == "user"
        assert user_message["content"] == "你好"
        assert user_message["status"] == "completed"
        assert assistant_message["role"] == "assistant"
        assert assistant_message["status"] == "completed"
        assert assistant_message["content"] == "你好"
        assert assistant_message["task_id"] == "t-1"

        # The four IDs reach map_core via forwarded headers.
        assert core.forwarded["X-Request-ID"]
        assert core.forwarded["X-Workspace-ID"] == WORKSPACE


async def test_same_request_id_replays_without_duplicate(app_and_core, session) -> None:
    app, _ = app_and_core
    async with await _client(app) as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]

        first = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "你好", "request_id": "req-dup"},
        )
        assert first.status_code == 200

        second = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "你好", "request_id": "req-dup"},
        )
        assert second.status_code == 200
        events = _parse_sse(second.content)
        assert events[-1][1].get("replayed") is True

        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assert len(detail["messages"]) == 2  # still exactly one pair


async def test_cross_user_conversation_is_404(app_and_core, session) -> None:
    app, core = app_and_core
    async with await _client(app) as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]

    # A trusted_header principal in a different workspace sees nothing.
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_conv_test_state.json",
            default_workspace_id="another-workspace",
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]
    async with await _client(other_app) as other_client:
        response = await other_client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers={"X-UserId": "other-user"},
        )
        assert response.status_code == 404


async def test_stream_failure_marks_message_failed(app_and_core, session) -> None:
    app, core = app_and_core
    core.fail = True
    async with await _client(app) as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]

        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "hi", "request_id": "req-fail"},
        )
        assert response.status_code == 200
        events = _parse_sse(response.content)
        assert any(event == "error" for event, _ in events)
        assert events[-1][1]["status"] == "failed"

        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assistant_message = detail["messages"][1]
        assert assistant_message["status"] == "failed"


async def test_stop_marks_streaming_message(app_and_core, session) -> None:
    app, _ = app_and_core
    async with await _client(app) as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]

        # Simulate a stuck streaming message: create pair directly via repo.
        from app.repositories.conversations import ConversationRepository

        repo = ConversationRepository(session)
        conversation = await repo.get_conversation(
            uuid.UUID(conversation_id),
            uuid.UUID(WORKSPACE),
            "local-admin",
        )
        _, assistant = await repo.create_message_pair(
            conversation=conversation, request_id="req-stuck", user_content="hi"
        )
        await session.commit()

        response = await client.post(f"/api/v1/messages/{assistant.id}:stop")
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
