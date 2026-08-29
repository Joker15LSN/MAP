"""Public /api/v1/runs* adapter for the Canonical Run module.

Router responsibilities end at protocol projection: parse identity/body,
call :class:`RunApplication`, map its typed errors to HTTP/SSE. It never
touches PG rows, leases or event ordering.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.identity import RequestPrincipal
from ..runs import RunApplication, RunCommand
from ..runs.errors import RunError, RunNotFoundError
from ..runtime.error_mapping import http_status_for, sse_error_frame
from ..services.runtime_snapshot.errors import RuntimeSnapshotUnavailableError
from .deps import get_principal, get_run_application, get_runtime_snapshots

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class CreateRunCommandRequest(BaseModel):
    kind: Literal["conversation_turn"]
    payload: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    command: CreateRunCommandRequest


class CancelRunRequest(BaseModel):
    reason: str = ""


def _workspace_uuid(principal: RequestPrincipal) -> uuid.UUID | None:
    try:
        return uuid.UUID(principal.workspace_id)
    except (ValueError, TypeError):
        return None


def _raise_run_error(exc: RunError) -> None:
    status = http_status_for(exc.code)
    if isinstance(exc, RunNotFoundError):
        status = 404
    raise HTTPException(
        status_code=status,
        detail=exc.message,
        headers={"X-MAP-Error-Code": exc.code},
    ) from exc


def _run_view_json(view) -> dict[str, Any]:
    return {
        "run_id": str(view.run_id),
        "workspace_id": str(view.workspace_id),
        "principal_id": view.principal_id,
        "conversation_id": str(view.conversation_id)
        if view.conversation_id is not None
        else None,
        "status": view.status,
        "command": view.command.to_json(),
        "last_seq": view.last_seq,
        "cancel_requested": view.cancel_requested,
        "error_code": view.error_code,
        "runtime_snapshot_id": str(view.runtime_snapshot_id)
        if view.runtime_snapshot_id is not None
        else None,
        "runtime_snapshot_digest": view.runtime_snapshot_digest,
    }


@router.post("", status_code=201)
async def create_run(
    payload: CreateRunRequest,
    principal: RequestPrincipal = Depends(get_principal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    application: RunApplication = Depends(get_run_application),
    snapshots=Depends(get_runtime_snapshots),
) -> dict[str, Any]:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required",
            headers={"X-MAP-Error-Code": "BAD_REQUEST"},
        )
    from ..services.idempotency import hash_request

    command = RunCommand(
        kind=payload.command.kind,
        payload=payload.command.payload,
        snapshot=payload.command.snapshot,
    )
    try:
        current_snapshot = await snapshots.get_current()
        if current_snapshot is None:
            raise RuntimeSnapshotUnavailableError()
        created = await application.create_run(
            workspace_id=workspace_id,
            principal_id=principal.user_id,
            conversation_id=payload.conversation_id,
            command=command,
            runtime_snapshot_id=current_snapshot.id,
            runtime_snapshot_digest=current_snapshot.digest,
            idempotency_key=idempotency_key,
            idempotency_body_hash=hash_request(payload.model_dump()),
        )
    except RunError as exc:
        _raise_run_error(exc)
    except RuntimeSnapshotUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=exc.message,
            headers={"X-MAP-Error-Code": exc.code},
        ) from exc
    return {
        "run_id": str(created.run_id),
        "status": created.status,
        "replayed": created.replayed,
    }


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    principal: RequestPrincipal = Depends(get_principal),
    application: RunApplication = Depends(get_run_application),
) -> dict[str, Any]:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    try:
        view = await application.get_run(
            workspace_id=workspace_id,
            principal_id=principal.user_id,
            run_id=run_id,
        )
    except RunError as exc:
        _raise_run_error(exc)
    return _run_view_json(view)


@router.get("/{run_id}/events")
async def replay_run_events(
    run_id: uuid.UUID,
    after_seq: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: RequestPrincipal = Depends(get_principal),
    application: RunApplication = Depends(get_run_application),
) -> StreamingResponse:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    resume_after = after_seq
    if last_event_id and last_event_id.isdigit():
        resume_after = max(resume_after, int(last_event_id))
    try:
        await application.get_run(
            workspace_id=workspace_id, principal_id=principal.user_id, run_id=run_id
        )
    except RunError as exc:
        _raise_run_error(exc)

    async def sse_stream():
        try:
            async for envelope in application.replay_events(
                workspace_id=workspace_id,
                principal_id=principal.user_id,
                run_id=run_id,
                after_seq=resume_after,
            ):
                yield envelope.sse_frame()
        except RunError as exc:
            yield sse_error_frame(exc.code, exc.message)

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}:cancel")
async def cancel_run(
    run_id: uuid.UUID,
    payload: CancelRunRequest,
    principal: RequestPrincipal = Depends(get_principal),
    application: RunApplication = Depends(get_run_application),
) -> dict[str, Any]:
    workspace_id = _workspace_uuid(principal)
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    try:
        receipt = await application.cancel_run(
            workspace_id=workspace_id,
            run_id=run_id,
            principal_id=principal.user_id,
            reason=payload.reason,
        )
    except RunError as exc:
        _raise_run_error(exc)
    return {
        "run_id": str(receipt.run_id),
        "accepted": receipt.accepted,
        "status": receipt.status,
    }
