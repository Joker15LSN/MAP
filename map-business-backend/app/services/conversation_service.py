"""Conversation streaming service (R1-CONV-01 / FIX-P1-CONV-01).

Pipeline per stream request:
1. idempotency: same ``request_id`` within (workspace, owner, conversation)
   -> the stored assistant message wins (no second pair ever created);
   concurrent identical request_id: unique violation is caught and
   re-queried safely (no 500, no orphan user message);
2. user message + streaming assistant placeholder committed atomically;
3. proxy map_core SSE with a cross-chunk incremental UTF-8 decoder and
   frame buffering; content deltas accumulate in memory and checkpoint on
   a fixed interval;
4. state machine ``pending -> streaming -> completed|failed|stopped``:
   only a legal ``done`` may complete; EOF without done, parse/decode
   errors, core error and client abort record distinct stable error codes;
5. stop (abort event) and done race: terminal updates are conditional, so
   at most one terminal state ever sticks;
6. R2-P1-01: the stream stays registered in the StreamRegistry from before
   upstream creation through consumption, parser close and finalize; stop
   actively cancels the upstream consumer task, and every exit path
   (done/error/stop/disconnect) unregisters in ONE outermost finally.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core_client import MapCoreClient
from ..db.models import Conversation
from ..repositories.conversations import ConversationRepository
from .runtime_payloads import build_runtime_chat_payload
from .sse import SseParseError, SseStreamParser, frame_data_json
from .stream_registry import StreamRegistry, drain_cancelled

CHECKPOINT_INTERVAL_S = 0.25

# Stable stream error codes (persisted in messages.stream_error).
STREAM_EOF_WITHOUT_DONE = "STREAM_EOF_WITHOUT_DONE"
STREAM_PARSE_ERROR = "STREAM_PARSE_ERROR"
STREAM_DECODE_ERROR = "STREAM_DECODE_ERROR"
STREAM_INVALID_DONE = "STREAM_INVALID_DONE"
STREAM_CORE_ERROR = "STREAM_CORE_ERROR"
STREAM_ABORTED = "STREAM_ABORTED"
STREAM_INTERRUPTED = "STREAM_INTERRUPTED"

# Frozen SSE event set (R1): start/meta/content_delta/done/error.
FROZEN_EVENTS = {"start", "meta", "content_delta", "done", "error"}


@dataclass
class StreamOutcome:
    status: str = "pending"  # pending | completed | failed | stopped
    content: str = ""
    task_id: str | None = None
    decision_json: dict | None = None
    evidence: list[dict] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    fallback_used: bool = False
    done_received: bool = False


async def stream_conversation_message(
    *,
    session: AsyncSession,
    conversation: Conversation,
    query: str,
    request_id: str,
    mode: str,
    store: Any,
    core_client: MapCoreClient,
    headers: dict[str, str],
    abort_event: asyncio.Event | None = None,
    checkpoint_interval_s: float = CHECKPOINT_INTERVAL_S,
    registry: StreamRegistry | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Proxy one user turn: persist pair, stream core SSE, checkpoint, finalize.

    Yields SSE-ready payload dicts (``event``/``data``) for the router.
    """
    repo = ConversationRepository(session)

    existing = await repo.find_message_by_request_id(
        request_id,
        conversation.workspace_id,
        conversation.owner_user_id,
        conversation.id,
    )
    if existing is not None:
        # Same request_id replayed inside the same conversation by the same
        # owner: return the stored assistant message without a second pair.
        yield {
            "event": "done",
            "data": {
                "message_id": str(existing.id),
                "content": existing.content,
                "replayed": True,
                "status": existing.status,
            },
        }
        return

    try:
        user_message, assistant_message = await repo.create_message_pair(
            conversation=conversation,
            request_id=request_id,
            user_content=query,
        )
        await session.commit()
    except IntegrityError:
        # Concurrent identical request_id: one pair wins, the loser rolls
        # back (no orphan user message) and re-queries safely.
        await session.rollback()
        existing = await repo.find_message_by_request_id(
            request_id,
            conversation.workspace_id,
            conversation.owner_user_id,
            conversation.id,
        )
        if existing is not None:
            yield {
                "event": "done",
                "data": {
                    "message_id": str(existing.id),
                    "content": existing.content,
                    "replayed": True,
                    "status": existing.status,
                },
            }
            return
        raise HTTPException(
            status_code=500,
            detail="concurrent request_id conflict could not be resolved",
            headers={"X-MAP-Error-Code": "INTERNAL_ERROR"},
        ) from None

    # Frozen event set: IDs travel in `start`.
    yield {
        "event": "start",
        "data": {
            "conversation_id": str(conversation.id),
            "message_id": str(assistant_message.id),
            "user_message_id": str(user_message.id),
        },
    }

    from ..schemas import ChatRequest

    payload = {"query": query, "history": []}
    runtime_payload = build_runtime_chat_payload(store, ChatRequest(**payload))

    # R2-P1-01: the registry entry spans the WHOLE pipeline — upstream
    # creation, pump, chunk consumption, parser close and finalize — and is
    # only removed by the outermost finally, so stop always finds a
    # mid-stream message.
    local_abort = abort_event
    if registry is not None:
        local_abort = registry.register(assistant_message.id)

    pump_task: asyncio.Task | None = None
    try:
        try:
            if mode == "flow":
                chunks = core_client.stream_chat_by_path(
                    "/flow_domain/chat/stream/v1",
                    runtime_payload,
                    headers=headers,
                )
            else:
                chunks = core_client.stream_chat(runtime_payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - proxy setup failure
            outcome = StreamOutcome(
                status="failed",
                error_code=STREAM_CORE_ERROR,
                error_message=str(exc),
                fallback_used=True,
            )
            await _finalize(repo, session, assistant_message.id, outcome)
            yield {
                "event": "error",
                "data": {"error": str(exc), "code": STREAM_CORE_ERROR, "fallback": True},
            }
            yield {"event": "done", "data": {"status": "failed", "content": ""}}
            return

        parser = SseStreamParser()
        outcome = StreamOutcome()
        accumulated = ""
        last_checkpoint = asyncio.get_running_loop().time()

        # The upstream runs in its own task feeding a queue: stop can cancel
        # it immediately (closing core's response, no further side effects)
        # instead of waiting for the next chunk boundary, and a hung
        # upstream can never wedge teardown.
        queue: asyncio.Queue[Any] = asyncio.Queue()
        pump_task = asyncio.create_task(_pump_upstream(chunks, queue))
        if registry is not None:
            registry.attach_consumer(assistant_message.id, pump_task)

        try:
            while True:
                item, aborted = await _next_upstream_item(queue, local_abort)
                if aborted:
                    # Stop requested: the registry already cancelled the
                    # upstream consumer, so core stops producing at once.
                    outcome = StreamOutcome(
                        status="stopped",
                        content=accumulated,
                        error_code=STREAM_ABORTED,
                    )
                    await _finalize(repo, session, assistant_message.id, outcome)
                    yield {
                        "event": "done",
                        "data": {"status": "stopped", "content": accumulated},
                    }
                    return
                if item is _UPSTREAM_EOF:
                    break
                if isinstance(item, _UpstreamError):
                    raise item.exc
                chunk = item

                parsed = parser.feed(chunk)  # raises SseParseError on bad UTF-8
                for frame in parsed.frames:
                    data = frame_data_json(frame)  # raises SseParseError on bad JSON
                    if frame.event == "content_delta" and isinstance(data.get("content"), str):
                        accumulated += data["content"]
                        yield {
                            "event": "content_delta",
                            "data": {"content": data["content"]},
                        }
                    elif frame.event == "meta" and data.get("type") == "decision":
                        outcome.decision_json = data
                    elif frame.event == "tool_evidence" or (
                        frame.event == "meta"
                        and data.get("type") in {"tool_observation", "evidence"}
                    ):
                        outcome.evidence.append(data)
                    elif frame.event == "done":
                        if outcome.done_received:
                            continue  # duplicate done: ignore, idempotent
                        outcome.done_received = True
                        done_data = data
                        outcome.content = str(done_data.get("content") or accumulated)
                        outcome.task_id = str(done_data.get("task_id") or "")
                    elif frame.event == "error":
                        if outcome.error_code is None:
                            outcome.error_code = STREAM_CORE_ERROR
                        outcome.error_message = str(data.get("error") or "upstream error")

                now = asyncio.get_running_loop().time()
                if now - last_checkpoint >= checkpoint_interval_s:
                    await repo.checkpoint_content(assistant_message.id, accumulated)
                    await session.commit()
                    last_checkpoint = now

            if not outcome.done_received:
                # EOF without a legal done: never completed.
                outcome = StreamOutcome(
                    status="failed",
                    content=accumulated,
                    error_code=STREAM_EOF_WITHOUT_DONE,
                    error_message="stream ended without done",
                )
                await _finalize(repo, session, assistant_message.id, outcome)
                yield {
                    "event": "error",
                    "data": {"error": "stream ended without done", "code": STREAM_EOF_WITHOUT_DONE},
                }
                yield {"event": "done", "data": {"status": "failed", "content": accumulated}}
                return

            if outcome.error_code is not None:
                # core sent error (then done): the error fact stays; a BFF
                # fallback would set fallback_used without erasing the code.
                outcome.status = "failed"
                outcome.content = outcome.content or accumulated
            else:
                outcome.status = "completed"
                outcome.content = outcome.content or accumulated
            await _finalize(repo, session, assistant_message.id, outcome)
            yield {
                "event": "done",
                "data": {
                    "message_id": str(assistant_message.id),
                    "content": outcome.content,
                    "status": outcome.status,
                    "task_id": outcome.task_id,
                },
            }
        except SseParseError as exc:
            outcome = StreamOutcome(
                status="failed",
                content=accumulated,
                error_code=exc.code,
                error_message=str(exc),
            )
            await _finalize(repo, session, assistant_message.id, outcome)
            yield {"event": "error", "data": {"error": str(exc), "code": exc.code}}
            yield {"event": "done", "data": {"status": "failed", "content": accumulated}}
        except asyncio.CancelledError:
            # Client disconnected / stream interrupted: finalize stopped via
            # a detached shielded write (the request session is already
            # dying), then re-raise.
            await _finalize_interrupted(assistant_message.id, accumulated)
            raise
        except Exception as exc:  # noqa: BLE001 - stream boundary; unexpected
            outcome = StreamOutcome(
                status="failed",
                content=accumulated,
                error_code=STREAM_CORE_ERROR,
                error_message=str(exc),
            )
            await _finalize(repo, session, assistant_message.id, outcome)
            yield {"event": "error", "data": {"error": str(exc), "code": STREAM_CORE_ERROR}}
            yield {"event": "done", "data": {"status": "failed", "content": accumulated}}
    finally:
        # Unified cleanup for explicit stop, client disconnect, upstream
        # done and upstream error: kill the pump if it is still running and
        # unregister exactly once — the registry never leaks a stream.
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
        await drain_cancelled(pump_task)
        if registry is not None:
            registry.unregister(assistant_message.id)


async def _aclose(chunks: AsyncIterator[Any]) -> None:
    """Close an async generator stream so upstream stops producing."""
    close = getattr(chunks, "aclose", None)
    if close is not None:
        with contextlib.suppress(Exception):
            await close()


_UPSTREAM_EOF = object()


class _UpstreamError:
    """Upstream exception carried across the pump queue by value."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


async def _pump_upstream(chunks: AsyncIterator[Any], queue: asyncio.Queue[Any]) -> None:
    """Forward upstream chunks to the queue; always deliver an EOF sentinel.

    Runs as a dedicated task so ``StreamRegistry.abort`` can cancel the
    upstream read directly: cancellation propagates into the core client
    generator whose finally closes the HTTP response/client, so core stops
    producing (no further tool calls or side effects). The queue is
    unbounded so the EOF sentinel in ``finally`` can never be lost.
    """
    try:
        async for chunk in chunks:
            queue.put_nowait(chunk)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised by the consumer
        queue.put_nowait(_UpstreamError(exc))
    finally:
        await _aclose(chunks)
        queue.put_nowait(_UPSTREAM_EOF)


async def _next_upstream_item(
    queue: asyncio.Queue[Any],
    abort_event: asyncio.Event | None,
) -> tuple[Any, bool]:
    """Race the next upstream item against the abort signal.

    Returns ``(item, aborted)``; ``aborted=True`` means stop fired before
    (or together with) the next item, so the caller finalizes ``stopped``
    without draining the already-cancelled upstream.
    """
    getter = asyncio.ensure_future(queue.get())
    if abort_event is None:
        return await getter, False
    aborter = asyncio.ensure_future(abort_event.wait())
    try:
        done, _pending = await asyncio.wait({getter, aborter}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        getter.cancel()
        aborter.cancel()
        raise
    if aborter in done:
        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await getter
        return None, True
    aborter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await aborter
    return getter.result(), False


async def _finalize(
    repo: ConversationRepository,
    session: AsyncSession,
    message_id: uuid.UUID,
    outcome: StreamOutcome,
) -> None:
    """Terminal write guarded by the streaming-state condition."""
    await repo.finalize_message(
        message_id,
        status=outcome.status,
        content=outcome.content,
        task_id=outcome.task_id,
        decision_json=outcome.decision_json,
        stream_error=outcome.error_code,
        error_message=outcome.error_message,
        fallback_used=outcome.fallback_used,
    )
    if outcome.evidence:
        await repo.add_evidence(message_id, outcome.evidence)
    await session.commit()


async def _finalize_interrupted(message_id: uuid.UUID, content: str) -> None:
    """Terminal write for abort paths where the request task is dying.

    The request session may already be in a cancelled state, so this runs
    detached (shielded, own session from the process engine) to guarantee
    the ``stopped`` terminal state lands in the DB exactly once (the write
    is conditional on status='streaming'). Best effort: the reconciler is
    the backstop if the process dies outright.
    """
    from ..db.session import get_session_factory

    async def _write() -> None:
        factory = get_session_factory()
        async with factory() as s:
            repo = ConversationRepository(s)
            await repo.finalize_message(
                message_id,
                status="stopped",
                content=content,
                stream_error=STREAM_ABORTED,
                error_message="stream interrupted",
            )
            await s.commit()

    with contextlib.suppress(Exception):
        await asyncio.shield(_write())
