"""Message feedback API (R1-FEEDBACK-01).

- PUT    /api/v1/messages/{id}/feedback            upsert (idempotent)
- DELETE /api/v1/messages/{id}/feedback/{kind}     remove
- GET    /api/v1/messages/{id}/feedback            current feedback
- POST   /api/v1/feedback/aggregate                batch summary
- GET    /api/v1/conversations/{id}/feedback-summary  conversation summary

Only the message owner (workspace + user) may submit/read feedback; any
other principal sees 404. Upserting the same kind twice is a no-op.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ..core.identity import RequestPrincipal
from ..db.models import MessageFeedback
from ..db.session import DbSession
from ..repositories.conversations import ConversationRepository
from .deps import get_principal
from .conversations import _workspace_uuid

router = APIRouter(prefix="/api/v1")

VALID_KINDS = {"thumbs_up", "thumbs_down"}


class FeedbackUpsertRequest(BaseModel):
    kind: str = Field(pattern="^(thumbs_up|thumbs_down)$")
    reason: str | None = Field(default=None, max_length=2000)


class FeedbackAggregateRequest(BaseModel):
    message_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


async def _owned_message(
    message_id: uuid.UUID, session, principal: RequestPrincipal
) -> Any:
    """Fetch the message and verify (workspace, owner) ownership; 404 else."""
    repo = ConversationRepository(session)
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="message not found")
    message = await repo.get_owned_message(message_id, workspace_id, principal.user_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return message


def _feedback_view(row: MessageFeedback) -> dict[str, Any]:
    return {
        "message_id": str(row.message_id),
        "kind": row.kind,
        "reason": row.reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.put("/messages/{message_id}/feedback")
async def upsert_feedback(
    message_id: uuid.UUID,
    payload: FeedbackUpsertRequest,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    await _owned_message(message_id, session, principal)
    workspace_id = _workspace_uuid(principal)
    existing = (
        await session.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.kind == payload.kind,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            MessageFeedback(
                message_id=message_id,
                workspace_id=workspace_id,
                kind=payload.kind,
                reason=payload.reason,
            )
        )
        await session.commit()
        created = (
            await session.execute(
                select(MessageFeedback).where(
                    MessageFeedback.message_id == message_id,
                    MessageFeedback.kind == payload.kind,
                )
            )
        ).scalar_one()
        return _feedback_view(created)
    # Idempotent upsert: same kind -> update reason, no duplicate row.
    existing.reason = payload.reason
    await session.commit()
    await session.refresh(existing)
    return _feedback_view(existing)


@router.delete("/messages/{message_id}/feedback/{kind}")
async def delete_feedback(
    message_id: uuid.UUID,
    kind: str,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, str]:
    if kind not in VALID_KINDS:
        raise HTTPException(status_code=404, detail="feedback not found")
    await _owned_message(message_id, session, principal)
    await session.execute(
        delete(MessageFeedback).where(
            MessageFeedback.message_id == message_id,
            MessageFeedback.kind == kind,
        )
    )
    await session.commit()
    return {"status": "deleted"}


@router.get("/messages/{message_id}/feedback")
async def get_feedback(
    message_id: uuid.UUID,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> list[dict[str, Any]]:
    await _owned_message(message_id, session, principal)
    rows = (
        await session.execute(
            select(MessageFeedback)
            .where(MessageFeedback.message_id == message_id)
            .order_by(MessageFeedback.created_at.asc())
        )
    ).scalars().all()
    return [_feedback_view(row) for row in rows]


@router.post("/feedback/aggregate")
async def aggregate_feedback(
    payload: FeedbackAggregateRequest,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    rows = (
        await session.execute(
            select(MessageFeedback).where(
                MessageFeedback.workspace_id == workspace_id,
                MessageFeedback.message_id.in_(payload.message_ids),
            )
        )
    ).scalars().all()
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = summary.setdefault(
            str(row.message_id), {"thumbs_up": 0, "thumbs_down": 0, "reasons": []}
        )
        entry[row.kind] += 1
        if row.reason:
            entry["reasons"].append(row.reason)
    return summary


@router.get("/conversations/{conversation_id}/feedback-summary")
async def conversation_feedback_summary(
    conversation_id: uuid.UUID,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    repo = ConversationRepository(session)
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    conversation = await repo.get_conversation(
        conversation_id, workspace_id, principal.user_id
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    messages = await repo.list_messages(conversation_id)
    assistant_ids = [m.id for m in messages if m.role == "assistant"]
    if not assistant_ids:
        return {"conversation_id": str(conversation_id), "thumbs_up": 0, "thumbs_down": 0}
    rows = (
        await session.execute(
            select(MessageFeedback.kind, func.count())
            .where(MessageFeedback.message_id.in_(assistant_ids))
            .group_by(MessageFeedback.kind)
        )
    ).all()
    counts = {"thumbs_up": 0, "thumbs_down": 0}
    for kind, count in rows:
        counts[kind] = count
    return {"conversation_id": str(conversation_id), **counts}
