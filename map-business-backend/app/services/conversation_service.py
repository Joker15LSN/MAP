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
   at most one terminal state ever sticks.
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
from .stream_registry import StreamRegistry

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

    local_abort = abort_event
    if registry is not None:
        local_abort = registry.register(assistant_message.id)
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
    finally:
        if registry is not None:
            registry.unregister(assistant_message.id)

    parser = SseStreamParser()
    outcome = StreamOutcome()
    accumulated = ""
    last_checkpoint = asyncio.get_running_loop().time()
    try:
        async for chunk in chunks:
            if local_abort is not None and local_abort.is_set():
                # Client requested stop: close the upstream stream so core
                # produces no further side effects.
                await _aclose(chunks)
                outcome = StreamOutcome(
                    status="stopped",
                    content=accumulated,
                    error_code=STREAM_ABORTED,
                )
                await _finalize(repo, session, assistant_message.id, outcome)
                yield {"event": "done", "data": {"status": "stopped", "content": accumulated}}
                return

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
                    frame.event == "meta" and data.get("type") in {"tool_observation", "evidence"}
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
        # Client disconnected: checkpoint and mark stopped, then re-raise.
        outcome = StreamOutcome(
            status="stopped",
            content=accumulated,
            error_code=STREAM_ABORTED,
        )
        await _finalize(repo, session, assistant_message.id, outcome)
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


async def _aclose(chunks: AsyncIterator[Any]) -> None:
    """Close an async generator stream so upstream stops producing."""
    close = getattr(chunks, "aclose", None)
    if close is not None:
        with contextlib.suppress(Exception):
            await close()


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
