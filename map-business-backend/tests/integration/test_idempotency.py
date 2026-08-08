"""F-03 acceptance: idempotency records against real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest

from app.db.models import IdempotencyRecord
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyService,
    hash_request,
)

pytestmark = pytest.mark.asyncio

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_same_key_same_body_replays_stored_response(session) -> None:
    service = IdempotencyService(session)
    body = {"query": "hello", "mode": "global"}
    key = "k-1"
    request_hash = hash_request(body)

    assert await service.lookup(
        key=key, workspace_id=WORKSPACE, principal_id="u1", request_hash=request_hash
    ) is None

    await service.store(
        key=key,
        workspace_id=WORKSPACE,
        principal_id="u1",
        request_hash=request_hash,
        response_status=200,
        response_body={"content": "ok"},
    )
    await session.commit()

    result = await service.lookup(
        key=key, workspace_id=WORKSPACE, principal_id="u1", request_hash=request_hash
    )
    assert result is not None
    assert result.replayed is True
    assert result.response_status == 200
    assert result.response_body == {"content": "ok"}


async def test_same_key_different_body_raises_conflict(session) -> None:
    service = IdempotencyService(session)
    key = "k-2"
    await service.store(
        key=key,
        workspace_id=WORKSPACE,
        principal_id="u1",
        request_hash=hash_request({"query": "a"}),
        response_status=200,
        response_body={"content": "a"},
    )
    await session.commit()

    with pytest.raises(IdempotencyConflictError):
        await service.lookup(
            key=key,
            workspace_id=WORKSPACE,
            principal_id="u1",
            request_hash=hash_request({"query": "b"}),
        )


async def test_same_key_different_principal_is_independent(session) -> None:
    service = IdempotencyService(session)
    key = "k-3"
    await service.store(
        key=key,
        workspace_id=WORKSPACE,
        principal_id="u1",
        request_hash=hash_request({"query": "a"}),
        response_status=200,
        response_body={"content": "a"},
    )
    await session.commit()

    # Another principal may reuse the same key with a different body.
    result = await service.lookup(
        key=key,
        workspace_id=WORKSPACE,
        principal_id="u2",
        request_hash=hash_request({"query": "b"}),
    )
    assert result is None


async def test_unique_constraint_prevents_duplicate_records(session) -> None:
    service = IdempotencyService(session)
    key = "k-4"
    request_hash = hash_request({"query": "x"})
    await service.store(
        key=key,
        workspace_id=WORKSPACE,
        principal_id="u1",
        request_hash=request_hash,
        response_status=200,
        response_body={},
    )
    await session.commit()

    # A concurrent duplicate insert must fail at the DB constraint level.
    from sqlalchemy.exc import IntegrityError

    session.add(
        IdempotencyRecord(
            workspace_id=WORKSPACE,
            principal_id="u1",
            key=key,
            request_hash=request_hash,
            response_status=200,
            response_body={},
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
