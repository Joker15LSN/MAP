"""Conversation / Message repository (R1-CONV-01).

Ownership rule: a conversation belongs to ``(workspace_id, owner_user_id)``;
reading or writing outside that pair must 404. The caller owns the
transaction; methods flush so the stream service can checkpoint in small
commits.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Conversation, Message, MessageEvidence


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc())
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
        self, request_id: str, workspace_id: uuid.UUID
    ) -> Message | None:
        result = await self._session.execute(
            select(Message).where(
                Message.request_id == request_id,
                Message.workspace_id == workspace_id,
                Message.role == "assistant",
            )
        )
        return result.scalar_one_or_none()

    async def checkpoint_content(self, message_id: uuid.UUID, content: str) -> None:
        """Frequent partial writes while streaming (cheap UPDATE by pk)."""
        message = await self._session.get(Message, message_id)
        if message is not None and message.status == "streaming":
            message.content = content
            message.version += 1
            await self._session.flush()

    async def finalize_message(
        self,
        message_id: uuid.UUID,
        *,
        status: str,
        content: str | None = None,
        task_id: str | None = None,
        decision_json: dict | None = None,
    ) -> Message | None:
        message = await self._session.get(Message, message_id)
        if message is None:
            return None
        message.status = status
        if content is not None:
            message.content = content
        if task_id:
            message.task_id = task_id
        if decision_json is not None:
            message.decision_json = decision_json
        message.completed_at = _utcnow()
        message.version += 1
        return message

    async def list_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        return list(result.scalars())

    async def get_message(
        self, message_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> Message | None:
        result = await self._session.execute(
            select(Message).where(
                Message.id == message_id,
                Message.workspace_id == workspace_id,
            )
        )
        return result.scalar_one_or_none()

    async def add_evidence(
        self, message_id: uuid.UUID, evidence: list[dict]
    ) -> None:
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
