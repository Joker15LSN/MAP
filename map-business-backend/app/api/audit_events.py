"""Non-repudiation audit API (FIX-P1-AUDIT-01).

- GET  /api/v1/admin/audit-events            filtered list (workspace scope)
- GET  /api/v1/admin/audit-events/verify     hash-chain verification

Read access requires ``platform_admin`` or ``audit_viewer``
(``require_audit_viewer``); queries carry the workspace predicate in SQL.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text

from ..core.identity import RequestPrincipal
from ..db.models import ConfigAuditEvent
from ..db.session import DbSession
from ..services.config_mutation import audit_record_payload, compute_entry_hash
from .conversations import _workspace_uuid
from .deps import require_audit_viewer

router = APIRouter(prefix="/api/v1/admin", tags=["audit"])


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
        "prev_entry_hash": row.prev_entry_hash,
        "entry_hash": row.entry_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/audit-events")
async def list_audit_events(
    session: DbSession,
    principal: RequestPrincipal = Depends(require_audit_viewer),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(applied|failed|rejected)$"),
    request_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
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
    count_sql = f"SELECT count(*) FROM ({sql}) AS sub"
    total = (
        await session.execute(text(count_sql), params)
    ).scalar_one()
    rows = (
        await session.execute(
            text(sql + " ORDER BY created_at DESC, id DESC LIMIT :lim OFFSET :off"),
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
    """Verify the entry_hash chain; report the first broken link."""
    rows = (
        await session.execute(
            select(ConfigAuditEvent).order_by(
                ConfigAuditEvent.created_at.asc(), ConfigAuditEvent.id.asc()
            )
        )
    ).scalars().all()
    prev = ""
    broken_at: str | None = None
    for index, row in enumerate(rows):
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
            error_message=None,
        )
        expected = compute_entry_hash(prev, record)
        if row.entry_hash != expected or row.prev_entry_hash != (prev or None):
            broken_at = f"{index}:{row.id}"
            break
        prev = row.entry_hash
    return {
        "ok": broken_at is None,
        "count": len(rows),
        "first_broken_at": broken_at,
    }
