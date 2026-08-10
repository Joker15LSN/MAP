"""FIX-P1-CONV-01 / R2-P1-01 acceptance: stream state machine, buffering,
stop, replay, registry lifecycle.

- byte-split streams: identical final events/content/status (E-03)
- EOF without done / error-then-done / bad JSON / duplicate done / abort
- stop vs done race (50 runs): exactly one terminal state, losing side
  stops executing, registry never leaks
- R2-P1-01: mid-stream registry hit, stop abort=True + upstream finally
  fired + side effects frozen; hung core cancelled within timeout;
  client disconnect cancels upstream; registry empty on every exit path
- reconciler marks stale streaming as interrupted, idempotent
- concurrent identical request_id -> exactly one pair
- user B replaying A's request_id -> 404, never A's content
- create conversation Idempotency-Key replay / conflict
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

os.environ.setdefault("MAP_BFF_STATE_FILE", "/tmp/map_bff_conv_fix_test_state.json")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.identity import AuthMode
from app.db.session import get_db_session
from app.main import create_app
from app.settings import Settings

pytestmark = pytest.mark.asyncio

WORKSPACE = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
STREAM = (
    'event: start\ndata: {"message_id":"m1"}\n\n'
    'event: content_delta\ndata: {"content":"你"}\n\n'
    'event: content_delta\ndata: {"content":"好"}\n\n'
    'event: done\ndata: {"content":"你好","task_id":"t-1"}\n\n'
).encode()


class FakeStreamCoreClient:
    """Core double: byte-splittable stream, abort/stop observation.

    R2-P1-01 observability:
    - ``side_effect_count`` increments once per produced chunk (stands in
      for core tool calls / external side effects); after a successful stop
      it must freeze;
    - ``closed`` is set in the upstream generator ``finally`` (fires both on
      natural exhaustion and on aclose/cancel);
    - ``completed_normally`` is only set when every chunk was consumed, so a
      cancelled stream can be told apart from a finished one.
    """

    def __init__(
        self,
        stream: bytes = STREAM,
        split: int | None = None,
        chunks: list[bytes] | None = None,
        chunk_delay_s: float = 0.0,
        hang_after: int | None = None,
        hang_seconds: float = 60.0,
    ) -> None:
        self.stream = stream
        self.split = split
        self.chunks = chunks
        self.chunk_delay_s = chunk_delay_s
        self.hang_after = hang_after
        self.hang_seconds = hang_seconds
        self.aborted = False
        self.fail_setup = False
        self.closed = asyncio.Event()
        self.completed_normally = False
        self.side_effect_count = 0

    def _chunks(self) -> list[bytes]:
        if self.chunks is not None:
            return list(self.chunks)
        if self.split is None:
            return [self.stream]
        return [self.stream[: self.split], self.stream[self.split :]]

    async def _stream(self):
        try:
            if self.fail_setup:
                raise RuntimeError("core down")
            for produced, chunk in enumerate(self._chunks()):
                if self.hang_after is not None and produced >= self.hang_after:
                    await asyncio.sleep(self.hang_seconds)  # core hang
                if self.chunk_delay_s:
                    await asyncio.sleep(self.chunk_delay_s)
                self.side_effect_count += 1  # one unit of core work
                yield chunk
            self.completed_normally = True
        finally:
            self.closed.set()

    async def stream_chat(self, payload, headers):
        async for c in self._stream():
            yield c

    async def stream_chat_by_path(self, path, payload, headers):
        async for c in self._stream():
            yield c


@pytest_asyncio.fixture
async def app_and_core(_engine, session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    core = FakeStreamCoreClient()
    app = create_app(
        settings=Settings(
            auth_mode=AuthMode.DEV,
            state_file="/tmp/map_bff_conv_fix_test_state.json",
            default_workspace_id=WORKSPACE,
        ),
        store=None,
        core_client=core,
    )
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _override
    app.state.test_factory = factory
    return app, core


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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


async def _new_conversation(client, title="测试"):
    response = await client.post("/api/v1/conversations", json={"mode": "global", "title": title})
    assert response.status_code == 201
    return response.json()["id"]


async def test_split_stream_matches_unsplit(app_and_core, session) -> None:
    """E-03: any split point yields identical events, content and status."""
    app, _ = app_and_core
    async with await _client(app) as client:
        for split in (3, 5, 7, 17, 31, 200):
            app.state.core_client = FakeStreamCoreClient(split=split)
            conversation_id = await _new_conversation(client)
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "你好", "request_id": f"req-split-{split}"},
            )
            events = _parse_sse(response.content)
            assert events[0][0] == "start"
            assert events[-1][0] == "done"
            assert events[-1][1]["status"] == "completed"
            assert events[-1][1]["content"] == "你好"

            detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
            assistant = detail["messages"][1]
            assert assistant["status"] == "completed"
            assert assistant["content"] == "你好"
            assert assistant["stream_error"] is None


async def test_eof_without_done_marks_failed(app_and_core, session) -> None:
    app, _ = app_and_core
    app.state.core_client = FakeStreamCoreClient(
        stream=b'event: start\ndata: {}\n\nevent: content_delta\ndata: {"content":"hi"}\n\n'
    )
    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "hi", "request_id": "req-nodone"},
        )
        events = _parse_sse(response.content)
        assert any(e == "error" for e, _ in events)
        assert events[-1][1]["status"] == "failed"

        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assistant = detail["messages"][1]
        assert assistant["status"] == "failed"
        assert assistant["stream_error"] == "STREAM_EOF_WITHOUT_DONE"
        assert assistant["fallback_used"] is False


async def test_error_then_done_keeps_error_fact(app_and_core, session) -> None:
    app, _ = app_and_core
    app.state.core_client = FakeStreamCoreClient(
        stream=(
            b'event: error\ndata: {"error":"upstream exploded"}\n\n'
            b'event: done\ndata: {"content":"partial"}\n\n'
        )
    )
    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "hi", "request_id": "req-err"},
        )
        events = _parse_sse(response.content)
        assert events[-1][1]["status"] == "failed"
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assistant = detail["messages"][1]
        assert assistant["status"] == "failed"
        assert assistant["stream_error"] == "STREAM_CORE_ERROR"
        assert assistant["fallback_used"] is False


async def test_bad_json_frame_marks_failed(app_and_core, session) -> None:
    app, _ = app_and_core
    app.state.core_client = FakeStreamCoreClient(
        stream=b"event: content_delta\ndata: {not-json}\n\nevent: done\ndata: {}\n\n"
    )
    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "hi", "request_id": "req-badjson"},
        )
        events = _parse_sse(response.content)
        assert events[-1][1]["status"] == "failed"
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assistant = detail["messages"][1]
        assert assistant["status"] == "failed"
        assert assistant["stream_error"] == "STREAM_PARSE_ERROR"


async def test_duplicate_done_is_idempotent(app_and_core, session) -> None:
    app, _ = app_and_core
    app.state.core_client = FakeStreamCoreClient(
        stream=(
            b'event: done\ndata: {"content":"once"}\n\nevent: done\ndata: {"content":"twice"}\n\n'
        )
    )
    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "hi", "request_id": "req-dupdone"},
        )
        events = _parse_sse(response.content)
        assert events[-1][1]["status"] == "completed"
        assert events[-1][1]["content"] == "once"
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assert detail["messages"][1]["content"] == "once"


async def test_stop_race_with_done_only_one_terminal_state(app_and_core, session) -> None:
    """50 runs: stop vs done race -> exactly one terminal state; the losing
    side stops executing (side effects freeze) and the registry never leaks."""
    app, _ = app_and_core
    registry = app.state.stream_registry

    outcomes = set()
    for run in range(50):
        core = FakeStreamCoreClient(
            stream=(
                b'event: content_delta\ndata: {"content":"a"}\n\n'
                b'event: content_delta\ndata: {"content":"b"}\n\n'
                b'event: done\ndata: {"content":"ab"}\n\n'
            ),
            split=1,  # many chunk boundaries -> many abort checkpoints
        )
        app.state.core_client = core

        async with await _client(app) as client:
            conversation_id = await _new_conversation(client)
            # Stream in the background; stop it after a tiny random delay.
            task = asyncio.create_task(
                client.post(
                    f"/api/v1/conversations/{conversation_id}/messages:stream",
                    json={"query": "hi", "request_id": f"req-race-{run}"},
                )
            )
            await asyncio.sleep(0.001 * (run % 5))
            stop_response = await client.post(
                f"/api/v1/messages/{await _assistant_id(app, conversation_id)}:stop"
            )
            await task
            assert stop_response.status_code == 200

            detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
            statuses = [m["status"] for m in detail["messages"] if m["role"] == "assistant"]
            assert statuses == ["stopped"] or statuses == ["completed"], statuses
            outcomes.add(statuses[0])

        # Losing side must not keep executing: once the stream task is over
        # the upstream is either exhausted or cancelled, so the side-effect
        # count freezes.
        for _ in range(100):
            if core.closed.is_set():
                break
            await asyncio.sleep(0.01)
        assert core.closed.is_set()
        first = core.side_effect_count
        await asyncio.sleep(0.02)
        assert core.side_effect_count == first
        # No registry leak, no pending consumer task.
        assert registry.active_count() == 0

    assert outcomes <= {"stopped", "completed"}


async def _assistant_id(app, conversation_id) -> uuid.UUID:
    from app.repositories.conversations import ConversationRepository

    # The pair is committed at stream start; poll briefly for it.
    for _ in range(50):
        async with app.state.test_factory() as s:
            repo = ConversationRepository(s)
            conversation = await repo.get_conversation(
                uuid.UUID(conversation_id), uuid.UUID(WORKSPACE), "local-admin"
            )
            if conversation is None:
                await asyncio.sleep(0.02)
                continue
            messages = await repo.list_messages(conversation.id)
            for m in messages:
                if m.role == "assistant":
                    return m.id
        await asyncio.sleep(0.02)
    raise AssertionError("assistant message never appeared")


def _slow_chunks(count: int = 20) -> list[bytes]:
    chunks = [
        f'event: content_delta\ndata: {{"content":"c{i}"}}\n\n'.encode() for i in range(count)
    ]
    content = "".join(f"c{i}" for i in range(count))
    chunks.append(f'event: done\ndata: {{"content":"{content}","task_id":"t-slow"}}\n\n'.encode())
    return chunks


async def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.005) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(interval_s)


async def test_stop_hits_registry_mid_stream_and_freezes_side_effects(
    app_and_core, session
) -> None:
    """R2-P1-01: mid-stream active_count==1 (registry must NOT unregister
    before the upstream is consumed); stop returns abort=True, upstream
    finally fires, and core side effects freeze afterwards."""
    app, _ = app_and_core
    registry = app.state.stream_registry
    core = FakeStreamCoreClient(chunks=_slow_chunks(), chunk_delay_s=0.03)
    app.state.core_client = core

    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        task = asyncio.create_task(
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "hi", "request_id": "req-stop-mid"},
            )
        )

        # Mid-stream: core has already produced chunks AND the registry
        # still holds the stream (the exact R2 regression).
        await _wait_until(lambda: core.side_effect_count >= 2 and registry.active_count() == 1)

        message_id = await _assistant_id(app, conversation_id)
        stop_response = await client.post(f"/api/v1/messages/{message_id}:stop")
        assert stop_response.status_code == 200
        assert stop_response.json()["abort"] is True

        response = await asyncio.wait_for(task, timeout=5)
        events = _parse_sse(response.content)
        assert events[-1][0] == "done"
        assert events[-1][1]["status"] == "stopped"

        # Upstream generator finally ran (core HTTP stream really closed)
        # and the stream did not run to natural completion.
        await _wait_until(core.closed.is_set)
        assert not core.completed_normally

        # Side effects freeze after stop: core keeps no producing.
        first = core.side_effect_count
        await asyncio.sleep(0.15)
        assert core.side_effect_count == first

        # Second stop after the terminal state: registry already empty.
        again = await client.post(f"/api/v1/messages/{message_id}:stop")
        assert again.status_code == 200
        assert again.json()["abort"] is False

    # Registry is exactly zero once the stream has ended.
    assert registry.active_count() == 0


async def test_stop_cancels_hung_upstream_within_timeout(app_and_core, session) -> None:
    """R2-P1-01: core hangs after the first chunk; stop must cancel the
    hung upstream read within the timeout instead of waiting for a chunk
    that never comes."""
    app, _ = app_and_core
    registry = app.state.stream_registry
    core = FakeStreamCoreClient(
        chunks=[
            b'event: content_delta\ndata: {"content":"h"}\n\n',
            b'event: done\ndata: {"content":"never"}\n\n',
        ],
        hang_after=1,
        hang_seconds=60,
    )
    app.state.core_client = core

    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        task = asyncio.create_task(
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "hi", "request_id": "req-hang"},
            )
        )
        # Core produced the first chunk and now sleeps inside the hang.
        await _wait_until(lambda: core.side_effect_count >= 1 and registry.active_count() == 1)
        await asyncio.sleep(0.02)

        message_id = await _assistant_id(app, conversation_id)
        started = asyncio.get_running_loop().time()
        stop_response = await client.post(f"/api/v1/messages/{message_id}:stop")
        assert stop_response.json()["abort"] is True
        response = await asyncio.wait_for(task, timeout=5)  # must not wait 60s
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 5

        events = _parse_sse(response.content)
        assert events[-1][1]["status"] == "stopped"
        await _wait_until(core.closed.is_set)
        assert not core.completed_normally

    assert registry.active_count() == 0


async def test_client_disconnect_cancels_upstream_and_unregisters(
    app_and_core, session
) -> None:
    """R2-P1-01: client disconnect follows the same cleanup path — upstream
    cancelled, registry unregistered, message stopped."""
    app, _ = app_and_core
    registry = app.state.stream_registry
    core = FakeStreamCoreClient(chunks=_slow_chunks(), chunk_delay_s=0.03)
    app.state.core_client = core

    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        task = asyncio.create_task(
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "hi", "request_id": "req-disconnect"},
            )
        )
        await _wait_until(lambda: core.side_effect_count >= 2 and registry.active_count() == 1)

        # Client goes away mid-stream.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Unified cleanup: upstream closed and registry drained.
        await _wait_until(lambda: core.closed.is_set() and registry.active_count() == 0)
        assert not core.completed_normally

        # The stopped terminal write is a shielded detached write (the
        # request session dies with the cancelled request): poll for it.
        async def _assistant_status():
            detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
            return next(m for m in detail["messages"] if m["role"] == "assistant")

        for _ in range(200):
            assistant = await _assistant_status()
            if assistant["status"] == "stopped":
                break
            await asyncio.sleep(0.02)
        assert assistant["status"] == "stopped"
        assert assistant["stream_error"] == "STREAM_ABORTED"


async def test_registry_empty_after_every_exit_path(app_and_core, session) -> None:
    """R2-P1-01: completed / error / EOF-without-done streams all leave the
    registry at exactly zero (no leaked entries, no pending consumers)."""
    app, _ = app_and_core
    registry = app.state.stream_registry
    cases = {
        "req-exit-ok": FakeStreamCoreClient(),
        "req-exit-err": FakeStreamCoreClient(
            stream=(
                b'event: error\ndata: {"error":"boom"}\n\n'
                b'event: done\ndata: {"content":"x"}\n\n'
            )
        ),
        "req-exit-nodone": FakeStreamCoreClient(
            stream=b'event: content_delta\ndata: {"content":"x"}\n\n'
        ),
    }
    async with await _client(app) as client:
        for request_id, core in cases.items():
            app.state.core_client = core
            conversation_id = await _new_conversation(client)
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "x", "request_id": request_id},
            )
            assert response.status_code == 200
            assert core.closed.is_set()
            assert registry.active_count() == 0, request_id


async def test_reconciler_marks_stale_streaming_interrupted(_engine, session) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.models import Message
    from app.repositories.conversations import ConversationRepository
    from app.services.message_reconciler import reconcile_stale_streaming_messages

    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        repo = ConversationRepository(s)
        conversation = await repo.create_conversation(
            workspace_id=uuid.UUID(WORKSPACE), owner_user_id="local-admin", mode="global", title="t"
        )
        await s.commit()
        _, assistant = await repo.create_message_pair(
            conversation=conversation, request_id="req-stale", user_content="hi"
        )
        # Make it look stale (updated long ago).
        await s.execute(
            sa.text("UPDATE map_control.messages SET updated_at = :ts WHERE id = :id"),
            {"ts": datetime.now(UTC) - timedelta(hours=1), "id": assistant.id},
        )
        await s.commit()

    async def _reconcile(stale_after_s: int) -> int:
        # R3-P0-01: the reconciler now runs on the caller's session and
        # flushes only; the caller owns the commit.
        async with factory() as s:
            count = await reconcile_stale_streaming_messages(s, stale_after_s=stale_after_s)
            await s.commit()
        return count

    count = await _reconcile(stale_after_s=60)
    assert count == 1

    # Idempotent: running again finds nothing.
    assert await _reconcile(stale_after_s=60) == 0

    async with factory() as s:
        message = await s.get(Message, assistant.id)
        assert message.status == "failed"
        assert message.stream_error == "STREAM_INTERRUPTED"

    # A fresh streaming message must not be touched.
    async with factory() as s:
        repo = ConversationRepository(s)
        conversation = await repo.create_conversation(
            workspace_id=uuid.UUID(WORKSPACE),
            owner_user_id="local-admin",
            mode="global",
            title="t2",
        )
        await s.commit()
        await repo.create_message_pair(
            conversation=conversation, request_id="req-fresh", user_content="hi"
        )
        await s.commit()
    assert await _reconcile(stale_after_s=3600) == 0


async def _latest_conversation_id(factory):
    from sqlalchemy import select

    from app.db.models import Conversation

    async with factory() as s:
        return (await s.execute(select(Conversation.id).limit(1))).scalar_one()


async def test_concurrent_same_request_id_single_pair(app_and_core, session) -> None:
    app, _ = app_and_core
    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        responses = await asyncio.gather(
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "hi", "request_id": "req-concurrent"},
            ),
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"query": "hi", "request_id": "req-concurrent"},
            ),
        )
        for response in responses:
            assert response.status_code == 200
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        messages = detail["messages"]
        assert len(messages) == 2  # exactly one user/assistant pair


async def test_other_user_cannot_replay_request_id(app_and_core, session) -> None:
    """User B using user A's request_id must never receive A's content."""
    app, _ = app_and_core
    async with await _client(app) as client:
        conversation_id = await _new_conversation(client)
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "secret content", "request_id": "req-owned"},
        )

    # User B (same workspace, different subject) tries the same request_id
    # inside their own conversation: B has no conversation with that id.
    other_app = create_app(
        settings=Settings(
            auth_mode=AuthMode.TRUSTED_HEADER,
            state_file="/tmp/map_bff_conv_fix_test_state.json",
            default_workspace_id=WORKSPACE,
            trusted_proxy_secret="s3cret",
            trusted_proxy_required=True,
        ),
        store=None,
        core_client=FakeStreamCoreClient(),
    )
    async with await _client(other_app) as other:
        # B cannot even see A's conversation.
        response = await other.post(
            f"/api/v1/conversations/{conversation_id}/messages:stream",
            json={"query": "hi", "request_id": "req-owned"},
            headers={"X-UserId": "user-B", "X-Trusted-Proxy-Secret": "s3cret"},
        )
        assert response.status_code == 404


async def test_create_conversation_idempotency_key(app_and_core, session) -> None:
    app, _ = app_and_core
    async with await _client(app) as client:
        first = await client.post(
            "/api/v1/conversations",
            json={"mode": "global", "title": "幂等"},
            headers={"Idempotency-Key": "conv-key-1"},
        )
        assert first.status_code == 201

        replay = await client.post(
            "/api/v1/conversations",
            json={"mode": "global", "title": "幂等"},
            headers={"Idempotency-Key": "conv-key-1"},
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]

        conflict = await client.post(
            "/api/v1/conversations",
            json={"mode": "flow", "title": "不同body"},
            headers={"Idempotency-Key": "conv-key-1"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

        listed = (await client.get("/api/v1/conversations")).json()
        assert len([c for c in listed if c["title"] == "幂等"]) == 1
