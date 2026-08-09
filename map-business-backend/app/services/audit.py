"""Admin write audit (R1-AUDIT-01).

``admin_write_guard`` is a FastAPI dependency (with teardown) attached to
every admin write route: it snapshots the admin state before the route
runs, then after the response is produced it records a single audit row
when the state actually changed. The actor always comes from the trusted
RequestPrincipal — a client-supplied ``operator`` field can never change it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, HTTPException, Request

from ..api.deps import get_store, require_admin
from ..core.identity import RequestPrincipal
from ..db.models import AuditLog
from ..db.session import DbSession
from ..repositories.config import ConfigRepository
from ..store import BadStateFileError

logger = logging.getLogger(__name__)


async def admin_write_guard(
    request: Request,
    session: DbSession,
    store: ConfigRepository = Depends(get_store),
    principal: RequestPrincipal = Depends(require_admin),
) -> AsyncIterator[RequestPrincipal]:
    """Gate + audit one admin write: 403 without platform_admin, one audit
    row per state-changing write, actor from the principal. Uses the
    request-scoped DB session so the write happens on the request's event
    loop."""
    try:
        before = store.load()
    except BadStateFileError as exc:
        # Fail closed AND leave a failed audit event (audit must never
        # disappear because the product write failed).
        try:
            from ..services.config_mutation import ConfigMutationService

            service = ConfigMutationService(store)  # type: ignore[arg-type]
            await service._append_event(  # private helper of the service
                session,
                workspace_id=uuid.UUID(principal.workspace_id)
                if _is_uuid(principal.workspace_id)
                else None,
                resource_type=_resource_type(request.url.path),
                resource_id=request.url.path,
                action="config.update",
                principal=principal,
                request=request,
                status="failed",
                failure_code="BAD_STATE_FILE",
                before_hash=None,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
        except Exception:  # never mask the original failure
            logger.exception("failed audit append during bad-state guard")
        raise HTTPException(
            status_code=500,
            detail=f"admin state file is corrupt and kept untouched: {exc}",
            headers={"X-MAP-Error-Code": "BAD_STATE_FILE"},
        ) from exc
    yield principal
    try:
        after = store.load()
        before_snapshot = _state_snapshot(before)
        after_snapshot = _state_snapshot(after)
        before_snapshot.pop("updated_at", None)
        after_snapshot.pop("updated_at", None)
        if before_snapshot == after_snapshot:
            return
        session.add(
            AuditLog(
                workspace_id=uuid.UUID(principal.workspace_id)
                if _is_uuid(principal.workspace_id)
                else uuid.UUID(int=0),
                actor_user_id=principal.user_id,
                action="config.update",
                resource_type=_resource_type(request.url.path),
                resource_id=request.url.path,
                request_id=getattr(request.state, "request_id", None),
                before_json=_state_snapshot(before),
                after_json=_state_snapshot(after),
            )
        )
        await session.commit()
    except Exception:  # audit must never break the admin write
        logger.exception("audit record failed for %s", request.url.path)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _resource_type(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[-1] in {"publish", "rollback", "upload", "refresh-tools"}:
        return f"{parts[-2]}.{parts[-1]}"
    return parts[-1] if parts else "unknown"


def _state_snapshot(state: Any) -> dict:
    try:
        return state.model_dump()
    except AttributeError:
        return {}
