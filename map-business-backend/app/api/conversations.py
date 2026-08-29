"""Conversation REST API (R1-CONV-01 / FIX-P1-CONV-01).

- POST /api/v1/conversations            create (Idempotency-Key aware)
- GET  /api/v1/conversations            list own conversations
- GET  /api/v1/conversations/{id}       restore detail + messages
- POST /api/v1/conversations/{id}/messages:stream   stream one turn (SSE)
- POST /api/v1/conversations/{id}/turns            start one canonical turn
- POST /api/v1/messages/{id}:stop       thin stop adapter (run cancel)

Ownership: a conversation belongs to (workspace_id, owner_user_id); any
other principal sees 404. SSE uses the frozen event set
start/meta/content_delta/done/error.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..core.identity import RequestPrincipal
from ..db.session import DbSession
from ..repositories.conversations import ConversationRepository
from ..schemas import (
    CreateConversationRequest,
    StartTurnRequest,
    StreamConversationMessageRequest,
)
from ..services.conversation_service import STREAM_ABORTED, stream_conversation_message
from ..services.idempotency import IdempotencyConflictError, IdempotencyService, hash_request
from ..services.runtime_snapshot.errors import RuntimeSnapshotUnavailableError
from ..turns import TurnApplication, TurnError, TurnNotFoundError
from .chat import _forward_headers
from .deps import get_core_client, get_principal, get_turn_application

router = APIRouter(prefix="/api/v1")


def _workspace_uuid(principal: RequestPrincipal) -> uuid.UUID | None:
    try:
        return uuid.UUID(principal.workspace_id)
    except ValueError:
        return None


def _to_conversation_view(c: Any) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "workspace_id": str(c.workspace_id),
        "mode": c.mode,
        "title": c.title,
        "status": c.status,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
    }


def _to_message_view(m: Any) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "role": m.role,
        "status": m.status,
        "content": m.content,
        "request_id": m.request_id,
        "task_id": m.task_id,
        "decision": m.decision_json,
        "run_id": str(m.run_id) if m.run_id else None,
        "stream_error": m.stream_error,
        "error_message": m.error_message,
        "fallback_used": m.fallback_used,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "completed_at": m.completed_at.isoformat() if m.completed_at else None,
    }


@router.post("/conversations", status_code=201)
async def create_conversation(
    payload: CreateConversationRequest,
    session: DbSession,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    body = {"mode": payload.mode, "title": payload.title}
    request_hash = hash_request(body)

    if idempotency_key:
        idempotency = IdempotencyService(session)
        try:
            stored = await idempotency.lookup(
                key=idempotency_key,
                workspace_id=workspace_id,
                principal_id=principal.user_id,
                request_hash=request_hash,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"X-MAP-Error-Code": "IDEMPOTENCY_CONFLICT"},
            ) from exc
        if stored is not None and stored.replayed:
            return stored.response_body or {}

    repo = ConversationRepository(session)
    conversation = await repo.create_conversation(
        workspace_id=workspace_id,
        owner_user_id=principal.user_id,
        mode=payload.mode,
        title=payload.title or "新会话",
    )
    await session.commit()
    view = _to_conversation_view(conversation)

    if idempotency_key:
        await idempotency.store(
            key=idempotency_key,
            workspace_id=workspace_id,
            principal_id=principal.user_id,
            request_hash=request_hash,
            response_status=201,
            response_body=view,
        )
        await session.commit()
    return view


@router.get("/conversations")
async def list_conversations(
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> list[dict[str, Any]]:
    repo = ConversationRepository(session)
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        return []
    conversations = await repo.list_conversations(
        workspace_id=workspace_id,
        owner_user_id=principal.user_id,
    )
    return [_to_conversation_view(c) for c in conversations]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    repo = ConversationRepository(session)
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    conversation = await repo.get_conversation(
        conversation_id,
        workspace_id=workspace_id,
        owner_user_id=principal.user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    messages = await repo.list_messages(conversation_id)
    return {
        **_to_conversation_view(conversation),
        "messages": [_to_message_view(m) for m in messages],
    }


@router.post("/conversations/{conversation_id}/messages:stream")
async def stream_message(
    conversation_id: uuid.UUID,
    payload: StreamConversationMessageRequest,
    request: Request,
    session: DbSession,
    request_token: str | None = Header(default=None, alias="X-request-token"),
    principal: RequestPrincipal = Depends(get_principal),
    core_client=Depends(get_core_client),
) -> StreamingResponse:
    repo = ConversationRepository(session)
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    conversation = await repo.get_conversation(
        conversation_id,
        workspace_id=workspace_id,
        owner_user_id=principal.user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    mode = payload.mode or conversation.mode
    request_id = (payload.request_id or "").strip()
    if not request_id:
        request_id = request.state.request_id

    headers = _forward_headers(request_token, request)
    store = request.app.state.store
    registry = request.app.state.stream_registry

    async def sse_stream():
        try:
            async for event in stream_conversation_message(
                session=session,
                conversation=conversation,
                query=payload.query,
                request_id=request_id,
                mode=mode,
                store=store,
                core_client=core_client,
                headers=headers,
                registry=registry,
            ):
                yield (
                    f"event: {event['event']}\n"
                    f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                ).encode()
        except Exception as exc:  # noqa: BLE001 - stream boundary
            error_data = json.dumps(
                {"error": str(exc), "conversation_id": str(conversation_id)},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n".encode()

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{conversation_id}/turns", status_code=201)
async def start_turn(
    conversation_id: uuid.UUID,
    payload: StartTurnRequest,
    request: Request,
    principal: RequestPrincipal = Depends(get_principal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    application: TurnApplication = Depends(get_turn_application),
) -> dict[str, Any]:
    """Start one canonical turn (Step 4 / PR-F1).

    Creates run + job + user/assistant messages + idempotency record in a
    single transaction and returns the durable identities the browser then
    uses to subscribe to ``/api/v1/runs/{run_id}/events``.
    """
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required",
            headers={"X-MAP-Error-Code": "BAD_REQUEST"},
        )
    request_id = (payload.request_id or "").strip() or request.state.request_id
    try:
        created = await application.start_turn(
            workspace_id=workspace_id,
            principal_id=principal.user_id,
            conversation_id=conversation_id,
            query=payload.query,
            request_id=request_id,
            idempotency_key=idempotency_key,
            idempotency_body_hash=hash_request(payload.model_dump()),
        )
    except TurnNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="conversation not found",
            headers={"X-MAP-Error-Code": exc.code},
        ) from exc
    except RuntimeSnapshotUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=exc.message,
            headers={"X-MAP-Error-Code": exc.code},
        ) from exc
    except TurnError as exc:
        raise HTTPException(
            status_code=409 if exc.code == "IDEMPOTENCY_CONFLICT" else 400,
            detail=exc.message,
            headers={"X-MAP-Error-Code": exc.code},
        ) from exc
    return {
        "run_id": str(created.run_id),
        "user_message_id": str(created.user_message_id),
        "assistant_message_id": str(created.assistant_message_id),
        "status": created.status,
        "replayed": created.replayed,
    }


@router.post("/messages/{message_id}:stop")
async def stop_message(
    message_id: uuid.UUID,
    session: DbSession,
    request: Request,
    principal: RequestPrincipal = Depends(get_principal),
    application: TurnApplication = Depends(get_turn_application),
) -> dict[str, Any]:
    """Thin stop adapter (Step 4 / PR-F1).

    For new turns (message.run_id is not NULL) the authoritative stop is
    ``POST /api/v1/runs/{run_id}:cancel`` via :class:`TurnApplication`.
    Legacy rows with no run keep the old ``StreamRegistry`` abort fallback
    so the retired path stays stoppable; new paths never touch the registry.
    """
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="message not found")
    try:
        receipt = await application.stop_turn(
            workspace_id=workspace_id,
            principal_id=principal.user_id,
            message_id=message_id,
        )
    except TurnError as exc:
        raise HTTPException(
            status_code=409,
            detail=exc.message,
            headers={"X-MAP-Error-Code": exc.code},
        ) from exc
    if receipt is None:
        raise HTTPException(status_code=404, detail="message not found")

    repo = ConversationRepository(session)
    message = await repo.get_owned_message(message_id, workspace_id, principal.user_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")

    aborted = receipt.accepted
    if receipt.run_id is None:
        # Legacy in-flight message without a canonical run: preserve the
        # registry abort fallback AND the conditional terminal write of the
        # retired path. New run-backed turns never enter this branch.
        aborted = request.app.state.stream_registry.abort(message_id)
        if message.status == "streaming":
            await repo.finalize_message(
                message_id,
                status="stopped",
                stream_error=STREAM_ABORTED,
                error_message="stopped by user",
            )
            await session.commit()
            message = await repo.get_owned_message(
                message_id, workspace_id, principal.user_id
            )
            assert message is not None
    return {
        **_to_message_view(message),
        "abort": aborted,
        "run_id": str(receipt.run_id) if receipt.run_id else None,
        "run_status": receipt.run_status,
    }
