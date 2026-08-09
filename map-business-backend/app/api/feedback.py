"""Message feedback API (R1-FEEDBACK-01 / FIX-P1-FEEDBACK-01).

- PUT    /api/v1/messages/{id}/feedback          create/overwrite own feedback
- GET    /api/v1/messages/{id}/feedback          own current feedback
- DELETE /api/v1/messages/{id}/feedback          withdraw (tombstone + outbox)
- POST   /api/v1/feedback/aggregate              counts for visible messages
- GET    /api/v1/conversations/{id}/feedback-summary
- GET    /api/v1/admin/feedback                  admin list (audit scope)
- POST   /api/v1/admin/feedback/{id}:convert-to-evaluation-case (R1-EVAL gate)
- DELETE /api/v1/messages/{id}/feedback/{kind}   legacy compatibility facade

Privacy: users only read/write their own feedback; aggregates expose counts
for messages the caller may see, never other users' reason text (E-04).
Correction/reason text is redacted before persistence.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ..core.identity import RequestPrincipal
from ..core.redaction import redact_text
from ..db.models import Conversation, Message, MessageFeedback, OutboxEvent
from ..db.session import DbSession
from ..repositories.conversations import ConversationRepository
from ..repositories.feedback import FeedbackRepository
from .conversations import _workspace_uuid
from .deps import get_principal, require_audit_viewer

router = APIRouter(prefix="/api/v1")

VALID_RATINGS = {"helpful", "unhelpful"}
VALID_REASON_CODES = {
    "incorrect",
    "outdated",
    "no_evidence",
    "not_relevant",
    "unsafe",
    "too_verbose",
    "tool_failed",
    "other",
}


class FeedbackUpsertRequest(BaseModel):
    rating: str = Field(pattern="^(helpful|unhelpful)$")
    reason_codes: list[str] = Field(default_factory=list, max_length=8)
    reason_other: str | None = Field(default=None, max_length=2000)
    correction_text: str | None = Field(default=None, max_length=8000)

    @field_validator("reason_codes")
    @classmethod
    def _valid_codes(cls, codes: list[str]) -> list[str]:
        invalid = set(codes) - VALID_REASON_CODES
        if invalid:
            raise ValueError(f"invalid reason_codes: {sorted(invalid)}")
        return codes

    @field_validator("reason_other")
    @classmethod
    def _other_required_when_other(cls, value: str | None, info) -> str | None:
        codes = info.data.get("reason_codes") or []
        if "other" in codes and not (value or "").strip():
            raise ValueError("reason_other is required when reason_codes contains 'other'")
        return value


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
        "id": str(row.id),
        "message_id": str(row.message_id),
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "rating": row.rating,
        "reason_codes": row.reason_codes or [],
        "reason_other": row.reason_other,
        "correction_text": row.correction_text,
        "status": row.status,
        "version": row.version,
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
    message = await _owned_message(message_id, session, principal)
    if message.role != "assistant":
        raise HTTPException(
            status_code=422,
            detail="feedback is only allowed on assistant messages",
            headers={"X-MAP-Error-Code": "VALIDATION_ERROR"},
        )
    if message.status != "completed":
        raise HTTPException(
            status_code=422,
            detail=f"feedback requires a completed message (status={message.status})",
            headers={"X-MAP-Error-Code": "VALIDATION_ERROR"},
        )
    workspace_id = _workspace_uuid(principal)
    repo = FeedbackRepository(session)
    row = await repo.upsert(
        message_id=message_id,
        workspace_id=workspace_id,
        conversation_id=message.conversation_id,
        request_id=message.request_id,
        user_id=principal.user_id,
        rating=payload.rating,
        reason_codes=sorted(set(payload.reason_codes)),
        reason_other=redact_text(payload.reason_other),
        correction_text=redact_text(payload.correction_text),
    )
    # RETURNING already carries this write's values; no refresh (a refresh
    # would read the newest DB value, which may belong to a concurrent PUT).
    await session.commit()
    return _feedback_view(row)


@router.get("/messages/{message_id}/feedback")
async def get_feedback(
    message_id: uuid.UUID,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any] | None:
    await _owned_message(message_id, session, principal)
    row = await FeedbackRepository(session).get_own(message_id, principal.user_id)
    if row is None:
        return None
    return _feedback_view(row)


@router.delete("/messages/{message_id}/feedback")
async def delete_feedback(
    message_id: uuid.UUID,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, str]:
    message = await _owned_message(message_id, session, principal)
    repo = FeedbackRepository(session)
    row = await repo.withdraw(message_id, principal.user_id)
    if row is not None:
        # Audit tombstone via the outbox (evidence survives; no physical
        # delete of audit-visible data).
        session.add(
            OutboxEvent(
                aggregate_type="message_feedback",
                aggregate_id=str(message_id),
                event_type="feedback_withdrawn",
                payload_json={
                    "message_id": str(message_id),
                    "conversation_id": str(message.conversation_id),
                    "user_id": principal.user_id,
                    "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
                },
            )
        )
    await session.commit()
    return {"status": "withdrawn"}


@router.post("/feedback/aggregate")
async def aggregate_feedback(
    payload: FeedbackAggregateRequest,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    """Counts for messages the caller may see; never other users' reasons."""
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    # Scope the messages to this user's conversations inside the SQL.
    visible_ids = (
        await session.execute(
            select(Message.id)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Message.id.in_(payload.message_ids),
                Message.workspace_id == workspace_id,
                Conversation.owner_user_id == principal.user_id,
            )
        )
    ).scalars().all()
    counts = await FeedbackRepository(session).count_by_message_ids(visible_ids)
    # Every requested (visible) message appears; missing ones are zeroed so
    # the helpful rate denominator is explicit.
    summary: dict[str, dict[str, int]] = {}
    for message_id in visible_ids:
        summary[str(message_id)] = counts.get(message_id, {"helpful": 0, "unhelpful": 0})
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
        return {"conversation_id": str(conversation_id), "helpful": 0, "unhelpful": 0}
    counts = await FeedbackRepository(session).count_by_message_ids(assistant_ids)
    helpful = sum(entry["helpful"] for entry in counts.values())
    unhelpful = sum(entry["unhelpful"] for entry in counts.values())
    return {"conversation_id": str(conversation_id), "helpful": helpful, "unhelpful": unhelpful}


@router.get("/admin/feedback")
async def admin_feedback_list(
    session: DbSession,
    principal: RequestPrincipal = Depends(require_audit_viewer),
    rating: str | None = Query(default=None, pattern="^(helpful|unhelpful)$"),
    reason_code: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    rows = await FeedbackRepository(session).list_admin(
        workspace_id=workspace_id,
        limit=limit,
        offset=offset,
        rating=rating,
        reason_code=reason_code,
    )
    return {"items": [_feedback_view(row) for row in rows], "count": len(rows)}


@router.post("/admin/feedback/{feedback_id}:convert-to-evaluation-case")
async def convert_to_evaluation_case(
    feedback_id: uuid.UUID,
    session: DbSession,
    principal: RequestPrincipal = Depends(require_audit_viewer),
) -> dict[str, Any]:
    """R1-EVAL gate: conversion needs the evaluation draft/version fact source.

    R1-EVAL is not implemented in this round, so the capability is
    explicitly unavailable behind a feature flag — never a fake case.
    """
    if os.getenv("MAP_EVAL_CONVERT_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        raise HTTPException(
            status_code=501,
            detail="convert-to-evaluation-case is unavailable until R1-EVAL provides "
            "dataset draft/version fact sources (MAP_EVAL_CONVERT_ENABLED=false)",
            headers={"X-MAP-Error-Code": "NOT_IMPLEMENTED"},
        )
    raise HTTPException(
        status_code=501,
        detail="R1-EVAL fact source not implemented",
        headers={"X-MAP-Error-Code": "NOT_IMPLEMENTED"},
    )


# --- legacy compatibility facade (old kind-based API keeps working) -----------

VALID_KINDS = {"thumbs_up", "thumbs_down"}


@router.delete("/messages/{message_id}/feedback/{kind}")
async def delete_feedback_legacy(
    message_id: uuid.UUID,
    kind: str,
    session: DbSession,
    principal: RequestPrincipal = Depends(get_principal),
) -> dict[str, str]:
    if kind not in VALID_KINDS:
        raise HTTPException(status_code=404, detail="feedback not found")
    await _owned_message(message_id, session, principal)
    await session.execute(
        MessageFeedback.__table__.delete().where(
            MessageFeedback.message_id == message_id,
            MessageFeedback.kind == kind,
            MessageFeedback.user_id.is_(None),  # only legacy rows
        )
    )
    await session.commit()
    return {"status": "deleted"}
