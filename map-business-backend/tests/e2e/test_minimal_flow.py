"""FIX-P2-CONTRACT-E2E-01:minimal browser->BFF->(core)->PostgreSQL flow.

One self-contained happy+privacy+recovery loop on a fresh database:
create (idempotency key) -> stream (fake core SSE) -> refresh restore ->
feedback -> admin list -> withdraw tombstone + outbox -> audit chain OK.

The browser is simulated with httpx; map_core is simulated by a fake SSE
client (core contract is pinned by map_core's own golden tests); MongoDB
projection is out of scope for the BFF E2E.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_e2e_state.json")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.asyncio

WORKSPACE = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


class FakeCore:
    async def stream_chat(self, payload, headers):
        yield (
            'event: start\ndata: {"message_id":"m"}\n\n'
            'event: content_delta\ndata: {"content":"你"}\n\n'
            'event: content_delta\ndata: {"content":"好"}\n\n'
            'event: done\ndata: {"content":"你好","task_id":"t-1"}\n\n'
        ).encode()

    async def stream_chat_by_path(self, path, payload, headers):
        async for chunk in self.stream_chat(payload, headers):
            yield chunk


@pytest_asyncio.fixture
async def e2e(_engine, session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file="/tmp/map_bff_e2e_state.json",
            default_workspace_id=WORKSPACE,
        ),
        store=None,
        core_client=FakeCore(),
    )
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override
    return app, session


async def test_minimal_flow(e2e) -> None:
    app, session = e2e
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. create with idempotency key; replay returns the same id.
        created = await client.post(
            "/api/v1/conversations",
            json={"mode": "global", "title": "E2E"},
            headers={"Idempotency-Key": "e2e-conv-key"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        replay = await client.post(
            "/api/v1/conversations",
            json={"mode": "global", "title": "E2E"},
            headers={"Idempotency-Key": "e2e-conv-key"},
        )
        assert replay.json()["id"] == conversation_id

        # 2. stream a message.
        stream = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "你好", "request_id": "e2e-req-1"},
        )
        assert stream.status_code == 200
        events = [line for line in stream.text.split("\n") if line.startswith("event:")]
        assert "event: start" in events
        assert events[-1] == "event: done"

        # 3. refresh restore shows the completed pair.
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        messages = detail["messages"]
        assert len(messages) == 2
        assistant = next(m for m in messages if m["role"] == "assistant")
        assert assistant["status"] == "completed"
        assert assistant["content"] == "你好"
        assert assistant["stream_error"] is None

        # 4. feedback.
        feedback = await client.put(
            f"/api/v1/messages/{assistant['id']}/feedback",
            json={"rating": "unhelpful", "reason_codes": ["incorrect"]},
        )
        assert feedback.status_code == 200
        assert feedback.json()["rating"] == "unhelpful"

        # 5. admin feedback list (workspace-scoped).
        listing = await client.get("/api/v1/admin/feedback")
        assert listing.status_code == 200
        assert listing.json()["count"] >= 1

        # 6. withdraw -> tombstone + outbox event.
        withdrawn = await client.delete(f"/api/v1/messages/{assistant['id']}/feedback")
        assert withdrawn.status_code == 200
        assert (await client.get(f"/api/v1/messages/{assistant['id']}/feedback")).json() is None

        # 7. an admin config write leaves an audit event; chain verifies.
        model = (await client.get("/api/admin/model-center")).json()
        saved = await client.put("/api/admin/model-center", json=model)
        assert saved.status_code == 200
        verify = await client.get("/api/v1/admin/audit-events/verify")
        assert verify.status_code == 200
        assert verify.json()["ok"] is True
        listing = await client.get("/api/v1/admin/audit-events")
        assert listing.json()["total"] >= 1

    # DB-level evidence.
    active_feedback = (
        await session.execute(
            text("SELECT count(*) FROM map_control.message_feedback WHERE status <> 'withdrawn'")
        )
    ).scalar_one()
    assert active_feedback == 0

    outbox = (
        await session.execute(
            text(
                "SELECT count(*) FROM map_control.outbox_events "
                "WHERE event_type = 'feedback_withdrawn'"
            )
        )
    ).scalar_one()
    assert outbox >= 1
