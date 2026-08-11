"""Idempotent request handling (F-03).

Same ``key`` + same request hash replays the stored response; same key with
a different request hash raises ``IdempotencyConflictError`` (HTTP 409).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import IdempotencyRecord


class IdempotencyConflictError(Exception):
    """The idempotency key was reused with a different request body."""


@dataclass
class IdempotencyResult:
    replayed: bool
    response_status: int
    response_body: dict | None


def hash_request(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup(
        self,
        *,
        key: str,
        workspace_id: uuid.UUID,
        principal_id: str,
        request_hash: str,
    ) -> IdempotencyResult | None:
        """Return the stored response, or raise on hash mismatch, or None."""
        result = await self._session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.workspace_id == workspace_id,
                IdempotencyRecord.principal_id == principal_id,
                IdempotencyRecord.key == key,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(
                f"idempotency key {key} reused with a different request body"
            )
        return IdempotencyResult(
            replayed=True,
            response_status=record.response_status,
            response_body=record.response_body,
        )

    async def store(
        self,
        *,
        key: str,
        workspace_id: uuid.UUID,
        principal_id: str,
        request_hash: str,
        response_status: int,
        response_body: dict | None,
        expires_at=None,
    ) -> None:
        self._session.add(
            IdempotencyRecord(
                workspace_id=workspace_id,
                principal_id=principal_id,
                key=key,
                request_hash=request_hash,
                response_status=response_status,
                response_body=response_body,
                expires_at=expires_at,
            )
        )
