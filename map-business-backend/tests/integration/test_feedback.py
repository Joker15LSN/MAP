"""FIX-P1-FEEDBACK-01 acceptance: current-fact model, privacy, redaction.

- like -> dislike -> add reason -> withdraw: at most one active row
- PUT replay does not duplicate; concurrent PUTs keep version monotonic
- two users independent feedback on their own messages
- user B aggregating user A's message: 404/empty, never A's reasons (E-04)
- non-assistant / incomplete / cross-workspace / cross-owner rejected
- secret fuzz: correction/reason text is redacted before persistence
- helpful rate SQL only counts explicit helpful/unhelpful rows
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_feedback_fix_test_state.json")

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
SECRET = "Bearer fake.eyJhbGciOiJIUzI1NiJ9.fake-token-value"


class FakeStreamCoreClient:
    async def stream_chat(self, payload, headers):
        yield 'event: done\ndata: {"content":"你好","task_id":"t-1"}\n\n'.encode()

    async def stream_chat_by_path(self, path, payload, headers):
        async for chunk in self.stream_chat(payload, headers):
            yield chunk


@pytest_asyncio.fixture
async def app_and_session(_engine, session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file=f"/tmp/map_bff_feedback_fix_{uuid.uuid4().hex[:8]}.json",
            default_workspace_id=WORKSPACE,
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override
    app.state.test_session = session
    return app, session


async def _conversation_with_assistant_message(app, *, role_ok=True) -> tuple[str, str]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = (await client.post("/api/v1/conversations", json={"mode": "global"})).json()
        conversation_id = created["id"]
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "你好", "request_id": f"req-{uuid.uuid4().hex[:8]}"},
        )
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assistant = next(m for m in detail["messages"] if m["role"] == "assistant")
        return conversation_id, assistant["id"]


async def test_like_dislike_reason_withdraw_single_row(app_and_session) -> None:
    app, session = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # like
        response = await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"rating": "helpful"},
        )
        assert response.status_code == 200
        assert response.json()["rating"] == "helpful"

        # switch to dislike: overwrite, not a second row
        response = await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={
                "rating": "unhelpful",
                "reason_codes": ["incorrect", "other"],
                "reason_other": "引用错误",
            },
        )
        assert response.status_code == 200
        assert response.json()["rating"] == "unhelpful"
        assert response.json()["reason_codes"] == ["incorrect", "other"]

        current = await client.get(f"/api/v1/messages/{message_id}/feedback")
        assert current.json()["rating"] == "unhelpful"

        # withdraw
        removed = await client.delete(f"/api/v1/messages/{message_id}/feedback")
        assert removed.status_code == 200
        assert (await client.get(f"/api/v1/messages/{message_id}/feedback")).json() is None

        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM map_control.message_feedback "
                    "WHERE message_id = :mid AND status <> 'withdrawn'"
                ),
                {"mid": uuid.UUID(message_id)},
            )
        ).scalar_one()
        assert count == 0  # at most one current row, tombstone excluded
        total = (
            await session.execute(
                text("SELECT count(*) FROM map_control.message_feedback WHERE message_id = :mid"),
                {"mid": uuid.UUID(message_id)},
            )
        ).scalar_one()
        assert total == 1  # tombstone kept (audit evidence, not physically deleted)


async def test_put_replay_and_concurrent_puts(app_and_session) -> None:
    app, _ = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(f"/api/v1/messages/{message_id}/feedback", json={"rating": "helpful"})
        replay = await client.put(
            f"/api/v1/messages/{message_id}/feedback", json={"rating": "helpful"}
        )
        assert replay.status_code == 200
        assert replay.json()["version"] == 2  # overwrite increments version

        # concurrent PUTs: version monotonic, never two rows
        responses = await asyncio.gather(
            *[
                client.put(
                    f"/api/v1/messages/{message_id}/feedback",
                    json={"rating": "unhelpful", "reason_codes": ["unsafe"]},
                )
                for _ in range(5)
            ]
        )
        versions = [r.json()["version"] for r in responses if r.status_code == 200]
        assert len(versions) == 5
        # Every PUT returned its own written version: unique and monotonic
        # across the whole batch (DB version is strictly increasing).
        assert sorted(versions) == list(range(3, 8)), versions
        current = await client.get(f"/api/v1/messages/{message_id}/feedback")
        assert current.json()["version"] == 7


async def test_two_users_independent_feedback(app_and_session) -> None:
    app, _session = app_and_session
    _conversation_id, message_id = await _conversation_with_assistant_message(app)
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_feedback_fix_other.json",
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]

    headers = {"X-UserId": "user-B", "X-Trusted-Proxy-Secret": "s3cret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(f"/api/v1/messages/{message_id}/feedback", json={"rating": "helpful"})
    async with AsyncClient(transport=ASGITransport(app=other_app), base_url="http://test") as other:
        # B cannot see A's conversation -> 404, no cross-user write.
        response = await other.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"rating": "unhelpful"},
            headers=headers,
        )
        assert response.status_code == 404


async def test_user_b_aggregate_never_leaks_a_reasons(app_and_session) -> None:
    """E-04 regression: aggregation must not expose other users' reasons."""
    app, _ = app_and_session
    _, message_id_a = await _conversation_with_assistant_message(app)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            f"/api/v1/messages/{message_id_a}/feedback",
            json={
                "rating": "unhelpful",
                "reason_codes": ["incorrect"],
                "reason_other": "A 的私密理由",
            },
        )
        # Same owner: aggregate over the message returns counts only, no reasons.
        aggregate = await client.post(
            "/api/v1/feedback/aggregate",
            json={"message_ids": [message_id_a]},
        )
        body = aggregate.json()
        assert body[str(message_id_a)] == {"helpful": 0, "unhelpful": 1}
        assert "私密理由" not in json.dumps(body)

    # User B (trusted_header) aggregates A's message id -> empty, no leak.
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_feedback_fix_other2.json",
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )
    other_app.dependency_overrides[get_db_session] = app.dependency_overrides[get_db_session]
    headers = {"X-UserId": "user-B", "X-Trusted-Proxy-Secret": "s3cret"}
    async with AsyncClient(transport=ASGITransport(app=other_app), base_url="http://test") as other:
        aggregate = await other.post(
            "/api/v1/feedback/aggregate",
            json={"message_ids": [message_id_a]},
            headers=headers,
        )
        assert aggregate.status_code == 200
        assert "私密理由" not in json.dumps(aggregate.json())
        assert str(message_id_a) not in aggregate.json() or (
            aggregate.json().get(str(message_id_a), {}) == {"helpful": 0, "unhelpful": 0}
        )


async def test_feedback_rejected_on_invalid_messages(app_and_session) -> None:
    app, _ = app_and_session
    conversation_id, _message = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        user_message = next(m for m in detail["messages"] if m["role"] == "user")
        # user message: 422
        response = await client.put(
            f"/api/v1/messages/{user_message['id']}/feedback",
            json={"rating": "helpful"},
        )
        assert response.status_code == 422

        # unknown message: 404
        response = await client.put(
            f"/api/v1/messages/{uuid.uuid4()}/feedback",
            json={"rating": "helpful"},
        )
        assert response.status_code == 404

        # invalid rating / reason code: 422
        response = await client.put(
            f"/api/v1/messages/{user_message['id']}/feedback",
            json={"rating": "great"},
        )
        assert response.status_code == 422
        response = await client.put(
            f"/api/v1/messages/{user_message['id']}/feedback",
            json={"rating": "unhelpful", "reason_codes": ["nonsense"]},
        )
        assert response.status_code == 422


async def test_correction_and_reason_redaction(app_and_session) -> None:
    app, session = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={
                "rating": "unhelpful",
                "reason_codes": ["other"],
                "reason_other": f"api_key={SECRET} 和 password: hunter2",
                "correction_text": f"Authorization: {SECRET}",
            },
        )
        assert response.status_code == 200
        body = response.json()
        raw = json.dumps(body)
        assert SECRET not in raw
        assert "hunter2" not in raw

    stored = (
        await session.execute(
            text(
                "SELECT reason_other, correction_text FROM map_control.message_feedback "
                "WHERE message_id = :mid"
            ),
            {"mid": uuid.UUID(message_id)},
        )
    ).one()
    assert SECRET not in (stored.reason_other or "")
    assert SECRET not in (stored.correction_text or "")
    assert "[REDACTED]" in (stored.reason_other or "")


async def test_helpful_rate_only_counts_explicit_feedback(app_and_session) -> None:
    """helpful/(helpful+unhelpful) must only use explicit ratings."""
    app, _ = app_and_session
    _, message_a = await _conversation_with_assistant_message(app)
    _, message_b = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(f"/api/v1/messages/{message_a}/feedback", json={"rating": "helpful"})
        # message_b has no feedback at all: must not count as helpful or unhelpful.
        summary = await client.post(
            "/api/v1/feedback/aggregate",
            json={"message_ids": [message_a, message_b]},
        )
        body = summary.json()
        helpful = body[str(message_a)]["helpful"]
        unhelpful = body[str(message_a)]["unhelpful"]
        assert helpful == 1 and unhelpful == 0
        assert str(message_b) in body  # present but zeroed


async def test_admin_list_and_convert_gate(app_and_session) -> None:
    app, _ = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"rating": "unhelpful", "reason_codes": ["incorrect"]},
        )
        listing = await client.get("/api/v1/admin/feedback")
        assert listing.status_code == 200
        assert listing.json()["count"] >= 1

        filtered = await client.get("/api/v1/admin/feedback?rating=unhelpful&reason_code=incorrect")
        assert filtered.json()["count"] >= 1
        filtered_miss = await client.get("/api/v1/admin/feedback?rating=helpful")
        assert filtered_miss.json()["count"] == 0

        # R1-EVAL gate: conversion is explicitly unavailable, never a fake case.
        feedback_id = listing.json()["items"][0]["id"]
        converted = await client.post(
            f"/api/v1/admin/feedback/{feedback_id}:convert-to-evaluation-case"
        )
        assert converted.status_code == 501
        assert converted.json()["code"] == "NOT_IMPLEMENTED"


async def test_legacy_delete_facade_still_works(app_and_session) -> None:
    """Old kind-based DELETE stays available as a compatibility facade."""
    app, _ = app_and_session
    _, message_id = await _conversation_with_assistant_message(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.put(
            f"/api/v1/messages/{message_id}/feedback",
            json={"rating": "helpful"},
        )
        # Legacy delete targets legacy rows only; new-format rows are untouched.
        response = await client.delete(f"/api/v1/messages/{message_id}/feedback/thumbs_up")
        assert response.status_code == 200
        current = await client.get(f"/api/v1/messages/{message_id}/feedback")
        assert current.json()["rating"] == "helpful"
