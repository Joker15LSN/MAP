"""Conversation / Message repository (R1-CONV-01 / FIX-P1-CONV-01).

Ownership rule: a conversation belongs to ``(workspace_id, owner_user_id)``;
reading or writing outside that pair must 404. The caller owns the
transaction. Terminal-state updates use conditional UPDATEs so a stopped
message can never be overwritten by a late ``done``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Conversation, Message, MessageEvidence


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_conversation(
        self,
        *,
        workspace_id: uuid.UUID,
        owner_user_id: str,
        mode: str,
        title: str,
    ) -> Conversation:
        conversation = Conversation(
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
            mode=mode,
            title=title,
        )
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def get_conversation(
        self, conversation_id: uuid.UUID, workspace_id: uuid.UUID, owner_user_id: str
    ) -> Conversation | None:
        result = await self._session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.workspace_id == workspace_id,
                Conversation.owner_user_id == owner_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_conversations(
        self, workspace_id: uuid.UUID, owner_user_id: str, limit: int = 50
    ) -> list[Conversation]:
        result = await self._session.execute(
            select(Conversation)
            .where(
                Conversation.workspace_id == workspace_id,
                Conversation.owner_user_id == owner_user_id,
            )
            .order_by(
                Conversation.last_message_at.desc().nulls_last(),
                Conversation.created_at.desc(),
            )
            .limit(limit)
        )
        return list(result.scalars())

    async def create_message_pair(
        self,
        *,
        conversation: Conversation,
        request_id: str,
        user_content: str,
    ) -> tuple[Message, Message]:
        """Persist the user message + streaming assistant placeholder atomically."""
        user_message = Message(
            conversation_id=conversation.id,
            workspace_id=conversation.workspace_id,
            role="user",
            status="completed",
            content=user_content,
            request_id=request_id,
            completed_at=_utcnow(),
        )
        assistant_message = Message(
            conversation_id=conversation.id,
            workspace_id=conversation.workspace_id,
            role="assistant",
            status="streaming",
            content="",
            request_id=request_id,
        )
        self._session.add(user_message)
        self._session.add(assistant_message)
        conversation.last_message_at = _utcnow()
        conversation.version += 1
        await self._session.flush()
        return user_message, assistant_message

    async def find_message_by_request_id(
        self,
        request_id: str,
        workspace_id: uuid.UUID,
        owner_user_id: str,
        conversation_id: uuid.UUID,
    ) -> Message | None:
        """Replay lookup scoped to workspace + owner + conversation.

        A known request_id from another user or another conversation must
        never return stored content.
        """
        result = await self._session.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Message.request_id == request_id,
                Message.role == "assistant",
                Message.workspace_id == workspace_id,
                Conversation.owner_user_id == owner_user_id,
                Message.conversation_id == conversation_id,
            )
        )
        return result.scalar_one_or_none()

    async def checkpoint_content(self, message_id: uuid.UUID, content: str) -> None:
        """Frequent partial writes while streaming (cheap UPDATE by pk)."""
        await self._session.execute(
            update(Message)
            .where(Message.id == message_id, Message.status == "streaming")
            .values(content=content, version=Message.version + 1)
        )

    async def finalize_message(
        self,
        message_id: uuid.UUID,
        *,
        status: str,
        content: str | None = None,
        task_id: str | None = None,
        decision_json: dict | None = None,
        stream_error: str | None = None,
        error_message: str | None = None,
        fallback_used: bool | None = None,
    ) -> bool:
        """Transition streaming -> terminal state (conditional UPDATE).

        Returns False when the message is no longer ``streaming`` (e.g. it
        was stopped first): a late ``done`` can never overwrite ``stopped``.
        """
        values: dict[str, Any] = {
            "status": status,
            "completed_at": _utcnow(),
            "version": Message.version + 1,
        }
        if content is not None:
            values["content"] = content
        if task_id:
            values["task_id"] = task_id
        if decision_json is not None:
            values["decision_json"] = decision_json
        if stream_error is not None:
            values["stream_error"] = stream_error
        if error_message is not None:
            values["error_message"] = error_message
        if fallback_used is not None:
            values["fallback_used"] = fallback_used
        result = await self._session.execute(
            update(Message)
            .where(Message.id == message_id, Message.status == "streaming")
            .values(**values)
        )
        return bool(result.rowcount)

    async def list_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        return list(result.scalars())

    async def get_owned_message(
        self,
        message_id: uuid.UUID,
        workspace_id: uuid.UUID,
        owner_user_id: str,
    ) -> Message | None:
        """Fetch a message the principal may access (workspace + owner scope)."""
        result = await self._session.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Message.id == message_id,
                Message.workspace_id == workspace_id,
                Conversation.owner_user_id == owner_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def add_evidence(self, message_id: uuid.UUID, evidence: list[dict]) -> None:
        for ordinal, item in enumerate(evidence):
            self._session.add(
                MessageEvidence(
                    message_id=message_id,
                    evidence_id=str(item.get("id") or item.get("evidence_id") or f"ev-{ordinal}"),
                    ordinal=ordinal,
                    evidence_json=item,
                )
            )
        await self._session.flush()
