"""Non-repudiation audit API (FIX-P1-AUDIT-01).

- GET  /api/v1/admin/audit-events            filtered list (workspace scope)
- GET  /api/v1/admin/audit-events/verify     hash-chain verification

Read access requires ``platform_admin`` or ``audit_viewer``
(``require_audit_viewer``); queries carry the workspace predicate in SQL.
Filters: resource_type, resource_id, actor, status, request_id, action
and the inclusive timezone-aware time range ``created_from``/``created_to``
(R3-P1-03).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text

from ..core.identity import RequestPrincipal
from ..db.models import ConfigAuditEvent
from ..db.session import DbSession
from ..services.config_mutation import audit_record_payload, compute_entry_hash
from .conversations import _workspace_uuid
from .deps import require_audit_viewer

router = APIRouter(prefix="/api/v1/admin", tags=["audit"])


def verify_chain_rows(rows) -> tuple[int, str | None]:
    """Walk the chain in ``ordinal`` order; return ``(count, first_broken)``.

    ``rows`` must already be ordered by ``ordinal`` ASC and expose the
    hash-canonical columns plus ``ordinal``, ``prev_entry_hash`` and
    ``entry_hash``. A row is broken when its ordinal is not contiguous,
    its ``prev_entry_hash`` does not link to the previous event, or its
    stored ``entry_hash`` differs from the recomputation — which uses the
    persisted ``error_message`` (one canonical schema for writer,
    reconciler and verifier, R2-P1-03).
    """
    prev = ""
    for index, row in enumerate(rows):
        if int(row.ordinal) != index or row.prev_entry_hash != prev:
            return len(rows), f"{index}:{row.id}"
        record = audit_record_payload(
            workspace_id=row.workspace_id,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            action=row.action,
            actor_user_id=row.actor_user_id,
            actor_subject=row.actor_subject,
            actor_roles=list(row.actor_roles or []),
            request_id=row.request_id,
            status=row.status,
            failure_code=row.failure_code,
            before_hash=row.before_hash,
            after_hash=row.after_hash,
            json_patch=row.json_patch,
            recovered=row.recovered,
            error_message=row.error_message,
        )
        if row.entry_hash != compute_entry_hash(prev, record):
            return len(rows), f"{index}:{row.id}"
        prev = row.entry_hash
    return len(rows), None


def _to_view(row: ConfigAuditEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id) if row.workspace_id else None,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "action": row.action,
        "actor_user_id": row.actor_user_id,
        "actor_subject": row.actor_subject,
        "actor_roles": row.actor_roles or [],
        "request_id": row.request_id,
        "before_version": row.before_version,
        "after_version": row.after_version,
        "json_patch": row.json_patch,
        "before_hash": row.before_hash,
        "after_hash": row.after_hash,
        "status": row.status,
        "failure_code": row.failure_code,
        "recovered": row.recovered,
        "error_message": row.error_message,
        "prev_entry_hash": row.prev_entry_hash,
        "entry_hash": row.entry_hash,
        "ordinal": row.ordinal,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _require_aware(value: datetime | None, name: str) -> datetime | None:
    """Time filters are timezone-aware ISO-8601 only (R3-P1-03): a naive
    timestamp is ambiguous against a timestamptz column and is rejected."""
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be a timezone-aware ISO-8601 timestamp",
            headers={"X-MAP-Error-Code": "INVALID_TIME_RANGE"},
        )
    return value


@router.get("/audit-events")
async def list_audit_events(
    session: DbSession,
    principal: RequestPrincipal = Depends(require_audit_viewer),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(applied|failed|rejected)$"),
    request_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    created_from: datetime | None = Query(
        default=None, description="Inclusive lower bound, timezone-aware ISO-8601"
    ),
    created_to: datetime | None = Query(
        default=None, description="Inclusive upper bound, timezone-aware ISO-8601"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    created_from = _require_aware(created_from, "created_from")
    created_to = _require_aware(created_to, "created_to")
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(
            status_code=422,
            detail="created_from must not be after created_to",
            headers={"X-MAP-Error-Code": "INVALID_TIME_RANGE"},
        )
    workspace_id = _workspace_uuid(principal)
    params: dict[str, Any] = {"ws": workspace_id}
    sql = "SELECT * FROM map_control.config_audit_events WHERE workspace_id = :ws"
    if resource_type:
        sql += " AND resource_type = :rt"
        params["rt"] = resource_type
    if resource_id:
        sql += " AND resource_id = :rid"
        params["rid"] = resource_id
    if actor:
        sql += " AND actor_user_id = :actor"
        params["actor"] = actor
    if status:
        sql += " AND status = :status"
        params["status"] = status
    if request_id:
        sql += " AND request_id = :req"
        params["req"] = request_id
    if action:
        sql += " AND action = :action"
        params["action"] = action
    if created_from is not None:
        sql += " AND created_at >= :cfrom"
        params["cfrom"] = created_from
    if created_to is not None:
        sql += " AND created_at <= :cto"
        params["cto"] = created_to
    count_sql = f"SELECT count(*) FROM ({sql}) AS sub"
    total = (await session.execute(text(count_sql), params)).scalar_one()
    rows = (
        await session.execute(
            text(sql + " ORDER BY ordinal DESC LIMIT :lim OFFSET :off"),
            {**params, "lim": limit, "off": offset},
        )
    ).all()
    return {
        "total": total,
        "items": [_to_view(row) for row in rows],
    }


@router.get("/audit-events/verify")
async def verify_audit_chain(
    session: DbSession,
    principal: RequestPrincipal = Depends(require_audit_viewer),
) -> dict[str, Any]:
    """Verify the entry_hash chain by ordinal; report the first broken link."""
    rows = (
        (
            await session.execute(
                select(ConfigAuditEvent).order_by(ConfigAuditEvent.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )
    count, broken_at = verify_chain_rows(rows)
    return {
        "ok": broken_at is None,
        "count": count,
        "first_broken_at": broken_at,
    }
