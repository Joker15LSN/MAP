"""R1-FEEDBACK-01 acceptance: message feedback upsert/aggregate/ownership.

Requires a conversation with an assistant message (created through the
conversation API with a fake core client).
"""

from __future__ import annotations

import json
import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_feedback_test_state.json")

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
    async def stream_chat(self, payload, headers):
        yield 'event: done\ndata: {"content":"你好","task_id":"t-1"}\n\n'.encode("utf-8")

    async def stream_chat_by_path(self, path, payload, headers):
        async for chunk in self.stream_chat(payload, headers):
            yield chunk


@pytest_asyncio.fixture
async def app_and_session(_engine, session):
    import uuid as _uuid

    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=f"/tmp/map_bff_feedback_state_{_uuid.uuid4().hex[:8]}.json",
            default_workspace_id=WORKSPACE,
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override
    return app, session


async def _conversation_with_assistant_message(app) -> tuple[str, str]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "你好", "request_id": f"req-{uuid.uuid4().hex[:8]}"},
        )
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assistant = next(m for m in detail["messages"] if m["role"] == "assistant")
        return conversation_id, assistant["id"]


async def test_upsert_feedback_is_idempotent(app_and_session) -> None:
    app, session = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"kind": "thumbs_up", "reason": "很准确"},
        )
        assert first.status_code == 200
        assert first.json()["kind"] == "thumbs_up"

        # Idempotent: same kind again updates, no duplicate row.
        second = await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"kind": "thumbs_up", "reason": "更新理由"},
        )
        assert second.status_code == 200

        current = await client.get(f"/api/v1/messages/{message_id}/feedback")
        rows = current.json()
        assert len(rows) == 1
        assert rows[0]["reason"] == "更新理由"


async def test_upsert_both_kinds_and_delete(app_and_session) -> None:
    app, _ = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            f"/api/v1/messages/{message_id}/feedback", json={"kind": "thumbs_up"}
        )
        await client.put(
            f"/api/v1/messages/{message_id}/feedback", json={"kind": "thumbs_down"}
        )
        rows = (await client.get(f"/api/v1/messages/{message_id}/feedback")).json()
        assert len(rows) == 2

        removed = await client.delete(
            f"/api/v1/messages/{message_id}/feedback/thumbs_down"
        )
        assert removed.status_code == 200
        rows = (await client.get(f"/api/v1/messages/{message_id}/feedback")).json()
        assert len(rows) == 1
        assert rows[0]["kind"] == "thumbs_up"


async def test_aggregate_counts(app_and_session) -> None:
    app, _ = app_and_session
    _, message_id_a = await _conversation_with_assistant_message(app)
    _, message_id_b = await _conversation_with_assistant_message(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            f"/api/v1/messages/{message_id_a}/feedback", json={"kind": "thumbs_up"}
        )
        await client.put(
            f"/api/v1/messages/{message_id_a}/feedback", json={"kind": "thumbs_down"}
        )
        await client.put(
            f"/api/v1/messages/{message_id_b}/feedback", json={"kind": "thumbs_up"}
        )

        aggregate = await client.post(
            "/api/v1/feedback/aggregate",
            json={"message_ids": [message_id_a, message_id_b]},
        )
        assert aggregate.status_code == 200
        summary = aggregate.json()
        assert summary[message_id_a] == {"thumbs_up": 1, "thumbs_down": 1, "reasons": []}
        assert summary[message_id_b]["thumbs_up"] == 1


async def test_conversation_feedback_summary(app_and_session) -> None:
    app, _ = app_and_session
    conversation_id, message_id = await _conversation_with_assistant_message(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            f"/api/v1/messages/{message_id}/feedback", json={"kind": "thumbs_up"}
        )
        summary = await client.get(
            f"/api/v1/conversations/{conversation_id}/feedback-summary"
        )
        assert summary.status_code == 200
        assert summary.json()["thumbs_up"] == 1
        assert summary.json()["thumbs_down"] == 0


async def test_cross_user_feedback_is_404(app_and_session) -> None:
    app, _ = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_feedback_state_other.json",
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]
    transport = ASGITransport(app=other_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"kind": "thumbs_up"},
            headers={"X-UserId": "other-user", "X-Trusted-Proxy-Secret": "s3cret"},
        )
        assert response.status_code == 404
