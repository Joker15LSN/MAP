"""Conversation streaming service (R1-CONV-01).

Pipeline per stream request:
1. idempotency: same ``request_id`` -> the stored assistant message wins
   (no second assistant message ever created);
2. user message + streaming assistant placeholder committed atomically;
3. proxy map_core SSE (global or flow), accumulate content deltas in
   memory, checkpoint every ~250ms;
4. ``done`` -> finalize completed; core error / BFF fallback -> failed;
   client abort -> stopped. At most one committed message stays in
   ``streaming`` after a crash (reconciler job later marks it failed).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core_client import MapCoreClient
from ..db.models import Conversation, Message
from ..repositories.conversations import ConversationRepository
from .runtime_payloads import build_runtime_chat_payload
from .sse import frame_data_json, parse_sse_frames

CHECKPOINT_INTERVAL_S = 0.25


@dataclass
class StreamOutcome:
    status: str = "pending"  # pending | completed | failed | stopped
    content: str = ""
    task_id: str | None = None
    decision_json: dict | None = None
    evidence: list[dict] = field(default_factory=list)
    error: str | None = None
    fallback_used: bool = False


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
) -> AsyncIterator[dict[str, Any]]:
    """Proxy one user turn: persist pair, stream core SSE, checkpoint, finalize.

    Yields SSE-ready payload dicts (``event``/``data``) for the router.
    """
    repo = ConversationRepository(session)

    existing = await repo.find_message_by_request_id(request_id, conversation.workspace_id)
    if existing is not None:
        # Same request_id replayed: return the stored assistant message
        # without creating a second one (idempotency contract).
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

    user_message, assistant_message = await repo.create_message_pair(
        conversation=conversation,
        request_id=request_id,
        user_content=query,
    )
    await session.commit()
    yield {
        "event": "message.started",
        "data": {
            "conversation_id": str(conversation.id),
            "user_message_id": str(user_message.id),
            "assistant_message_id": str(assistant_message.id),
        },
    }

    from ..schemas import ChatRequest

    payload = {"query": query, "history": []}
    runtime_payload = build_runtime_chat_payload(store, ChatRequest(**payload))

    outcome = StreamOutcome()
    accumulated = ""
    try:
        if mode == "flow":
            chunks = core_client.stream_chat_by_path(
                "/flow_domain/chat/stream/v1",
                runtime_payload,
                headers=headers,
            )
        else:
            chunks = core_client.stream_chat(runtime_payload, headers=headers)
    except Exception as exc:  # proxy setup failure
        outcome = StreamOutcome(
            status="failed",
            error=str(exc),
            fallback_used=True,
        )
        await _finalize(repo, session, assistant_message.id, outcome)
        yield {"event": "error", "data": {"error": str(exc), "fallback": True}}
        yield {"event": "done", "data": {"status": "failed", "content": ""}}
        return

    last_checkpoint = asyncio.get_running_loop().time()
    try:
        async for chunk in chunks:
            if abort_event is not None and abort_event.is_set():
                outcome = StreamOutcome(status="stopped", content=accumulated)
                await _finalize(repo, session, assistant_message.id, outcome)
                yield {
                    "event": "done",
                    "data": {"status": "stopped", "content": accumulated},
                }
                return

            text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            for frame in parse_sse_frames(text).frames:
                data = frame_data_json(frame)
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
                    done_data = data
                    outcome.content = str(
                        done_data.get("content") or accumulated
                    )
                    outcome.task_id = str(done_data.get("task_id") or "")
                elif frame.event == "error":
                    outcome.error = str(data.get("error") or "upstream error")

            # Periodic checkpoint: flush accumulated deltas to the DB.
            now = asyncio.get_running_loop().time()
            if now - last_checkpoint >= checkpoint_interval_s:
                await repo.checkpoint_content(assistant_message.id, accumulated)
                await session.commit()
                last_checkpoint = now

        if outcome.error is not None:
            outcome.status = "failed"
            outcome.content = outcome.content or accumulated
            outcome.fallback_used = True
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
    except asyncio.CancelledError:
        # Client disconnected: mark stopped at the last checkpoint.
        outcome = StreamOutcome(status="stopped", content=accumulated)
        await _finalize(repo, session, assistant_message.id, outcome)
        raise
    except Exception as exc:  # unexpected stream failure
        outcome = StreamOutcome(status="failed", content=accumulated, error=str(exc))
        await _finalize(repo, session, assistant_message.id, outcome)
        yield {"event": "error", "data": {"error": str(exc)}}
        yield {"event": "done", "data": {"status": "failed", "content": accumulated}}


async def _finalize(
    repo: ConversationRepository,
    session: AsyncSession,
    message_id: uuid.UUID,
    outcome: StreamOutcome,
) -> None:
    await repo.finalize_message(
        message_id,
        status=outcome.status,
        content=outcome.content,
        task_id=outcome.task_id,
        decision_json=outcome.decision_json,
    )
    if outcome.evidence:
        await repo.add_evidence(message_id, outcome.evidence)
    await session.commit()
