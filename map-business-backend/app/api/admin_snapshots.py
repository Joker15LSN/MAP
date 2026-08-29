"""Admin runtime snapshot lifecycle routes (Step 7 PR-J4).

Read and mutate the PG-backed runtime snapshot registry. Every route is
protected by ``admin_write_guard``; lifecycle writes go through
``RuntimeSnapshotService`` so each operation appends exactly one snapshot
audit event and an outbox event (service semantics).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..core.identity import RequestPrincipal
from ..db.session import DbSession
from ..services.audit import admin_write_guard
from ..services.runtime_snapshot.schemas import MutationContext, RuntimeSnapshotRecord
from .deps import get_runtime_snapshots

router = APIRouter()


class ActivateSnapshotRequest(BaseModel):
    """Minimal activation body.

    ``expected_current_digest`` is optional. When omitted, the route
    derives the expectation from the server-side current pointer
    (fail-closed): it never silently passes ``None`` when a current
    snapshot exists.
    """

    expected_current_digest: str | None = None


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="runtime config snapshot not found",
        headers={"X-MAP-Error-Code": "SNAPSHOT_NOT_FOUND"},
    )


def _context(
    request: Request,
    principal: RequestPrincipal,
    snapshot_id: uuid.UUID,
    action: str,
) -> MutationContext:
    return MutationContext(
        principal=principal,
        request=request,
        resource_type="runtime_snapshot",
        resource_id=str(snapshot_id),
        action=action,
    )


@router.get("/api/admin/runtime-snapshots/current")
async def get_current_runtime_snapshot(
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
    snapshots=Depends(get_runtime_snapshots),
) -> RuntimeSnapshotRecord:
    """Return the current active snapshot record (404 when none)."""
    record = await snapshots.get_current()
    if record is None:
        raise _not_found()
    return record


@router.get("/api/admin/runtime-snapshots/{snapshot_id}")
async def get_runtime_snapshot(
    snapshot_id: uuid.UUID,
    session: DbSession,
    _: RequestPrincipal = Depends(admin_write_guard),
    snapshots=Depends(get_runtime_snapshots),
) -> RuntimeSnapshotRecord:
    """Return one snapshot record by id (404 when missing)."""
    record = await snapshots.get(snapshot_id)
    if record is None:
        raise _not_found()
    return record


@router.post("/api/admin/runtime-snapshots/{snapshot_id}/publish")
async def publish_runtime_snapshot(
    snapshot_id: uuid.UUID,
    request: Request,
    session: DbSession,
    principal: RequestPrincipal = Depends(admin_write_guard),
    snapshots=Depends(get_runtime_snapshots),
) -> RuntimeSnapshotRecord:
    """Publish a draft snapshot."""
    return await snapshots.publish(
        session, snapshot_id, _context(request, principal, snapshot_id, "publish")
    )


@router.post("/api/admin/runtime-snapshots/{snapshot_id}/activate")
async def activate_runtime_snapshot(
    snapshot_id: uuid.UUID,
    request: Request,
    session: DbSession,
    payload: ActivateSnapshotRequest | None = None,
    principal: RequestPrincipal = Depends(admin_write_guard),
    snapshots=Depends(get_runtime_snapshots),
) -> RuntimeSnapshotRecord:
    """Activate a published/rolled_back snapshot (CAS on current digest)."""
    expected_current_digest = payload.expected_current_digest if payload else None
    if expected_current_digest is None:
        # Fail-closed: derive the expectation from the current pointer when
        # the caller did not provide one. This is still a CAS — a racing
        # activation that changes the pointer after the read loses with 409.
        current = await snapshots.get_current()
        expected_current_digest = current.digest if current else None
    return await snapshots.activate(
        session,
        snapshot_id,
        expected_current_digest,
        _context(request, principal, snapshot_id, "activate"),
    )


@router.post("/api/admin/runtime-snapshots/{snapshot_id}/rollback")
async def rollback_runtime_snapshot(
    snapshot_id: uuid.UUID,
    request: Request,
    session: DbSession,
    principal: RequestPrincipal = Depends(admin_write_guard),
    snapshots=Depends(get_runtime_snapshots),
) -> RuntimeSnapshotRecord:
    """Roll the current pointer back to ``snapshot_id`` (CAS)."""
    return await snapshots.rollback(
        session, snapshot_id, _context(request, principal, snapshot_id, "rollback")
    )


@router.post("/api/admin/runtime-snapshots/{snapshot_id}/retire")
async def retire_runtime_snapshot(
    snapshot_id: uuid.UUID,
    request: Request,
    session: DbSession,
    principal: RequestPrincipal = Depends(admin_write_guard),
    snapshots=Depends(get_runtime_snapshots),
) -> RuntimeSnapshotRecord:
    """Retire a non-active snapshot."""
    return await snapshots.retire(
        session, snapshot_id, _context(request, principal, snapshot_id, "retire")
    )
