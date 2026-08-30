"""Runtime snapshot lifecycle service (Step 7 PR-J2 / J7a).

``apply_change`` runs the whole admin write as ONE PostgreSQL transaction:
lock the PG admin state row -> validate fail-closed -> pure
``build_draft`` computation -> project/digest -> insert draft snapshot ->
publish -> activate (CAS on the current pointer) -> save admin state ->
admin audit + snapshot audit + outbox -> commit. A snapshot CAS failure
rolls the whole transaction back (the admin state is NOT persisted) and
records a failed snapshot audit; an audit append failure rolls the whole
transaction back and returns 500 AUDIT_WRITE_FAILED. No pending mutation
row is needed anymore because PG atomicity replaces the file-rename
window; the legacy reconcilers remain only to drain rows written by older
versions.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.identity import RequestPrincipal
from ...core.redaction import redact_payload
from ...db.models import OutboxEvent
from ..config_mutation import AuditWriteError, append_audit_event
from ..json_diff import json_patch_diff
from .adapters.admin_state_pg import PgAdminStateRepository
from .digest import projection_digest, snapshot_id_for_digest, state_hash
from .errors import (
    AdminStateUnavailableError,
    BadAdminStateError,
    SnapshotConcurrentModificationError,
    SnapshotStateConflictError,
)
from .repository import RuntimeSnapshotRepository
from .schemas import (
    MutationContext,
    RuntimeSnapshotRecord,
    build_runtime_projection,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _workspace_uuid(principal: RequestPrincipal) -> Any:
    try:
        return uuid.UUID(principal.workspace_id)
    except (ValueError, TypeError):
        return None


def _request_id(request: Any) -> str | None:
    request_state = getattr(request, "state", None)
    if request_state is None:
        return None
    return getattr(request_state, "request_id", None)


class RuntimeSnapshotService:
    def __init__(
        self,
        admin_store: PgAdminStateRepository,
        snapshots: RuntimeSnapshotRepository,
    ) -> None:
        self._admin_store = admin_store
        self._snapshots = snapshots

    async def apply_change(
        self,
        session: AsyncSession,
        context: MutationContext,
        build_draft: Callable[[Any], T],
    ) -> tuple[Any, T]:
        """Audited snapshot write from an AdminState change (one PG tx)."""
        # 1. Lock the singleton admin state row and validate it fail-closed.
        try:
            current = await self._admin_store.load()
        except AdminStateUnavailableError as exc:
            await self._append_admin_audit(
                session,
                context,
                status="failed",
                failure_code="ADMIN_STATE_UNAVAILABLE",
                before_hash=None,
                after_hash=None,
                json_patch=None,
                error_message=str(exc),
            )
            await session.commit()
            raise HTTPException(
                status_code=500,
                detail=f"admin state unavailable: {exc}",
                headers={"X-MAP-Error-Code": "ADMIN_STATE_UNAVAILABLE"},
            ) from exc
        except BadAdminStateError as exc:
            await self._append_admin_audit(
                session,
                context,
                status="failed",
                failure_code="BAD_ADMIN_STATE",
                before_hash=None,
                after_hash=None,
                json_patch=None,
                error_message=str(exc),
            )
            await session.commit()
            raise HTTPException(
                status_code=500,
                detail=f"admin state is corrupt and kept untouched: {exc}",
                headers={"X-MAP-Error-Code": "BAD_ADMIN_STATE"},
            ) from exc
        before_admin_hash = state_hash(current)
        before_state = current.model_copy(deep=True)

        # 2. Pure computation: the updater mutates the in-memory draft only.
        try:
            result = build_draft(current)
        except HTTPException as exc:
            failure_code = "BUSINESS_REJECTED"
            if getattr(exc, "headers", None) and exc.headers.get("X-MAP-Error-Code"):
                failure_code = exc.headers["X-MAP-Error-Code"]
            await self._append_admin_audit(
                session,
                context,
                status="rejected",
                failure_code=failure_code,
                before_hash=before_admin_hash,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise

        current.updated_at = datetime.now().isoformat()
        target_admin_hash = state_hash(current)
        projection = build_runtime_projection(current)
        target_digest = projection_digest(projection)
        snapshot_id = snapshot_id_for_digest(target_digest)
        before_sanitized = redact_payload(before_state.model_dump())
        after_sanitized = redact_payload(current.model_dump())
        patch = json_patch_diff(before_sanitized, after_sanitized)

        current_snapshot = await self._snapshots.get_current()
        expected_current_digest = current_snapshot.digest if current_snapshot else None
        parent_id = current_snapshot.id if current_snapshot else None

        # 3. Snapshot side + admin state + audits + outbox in ONE transaction.
        try:
            inserted = await self._snapshots.insert(
                snapshot_id, projection, target_digest, parent_id, "draft"
            )
            if inserted.status == "draft":
                await self._snapshots.transition_status(snapshot_id, "draft", "published")
            if inserted.status in ("draft", "published", "active", "rolled_back"):
                # ``rolled_back`` is a valid restart: an admin write that
                # reproduces an earlier projection reactivates that snapshot
                # and rolls the current active one back.
                await self._snapshots.activate(snapshot_id, expected_current_digest)
            else:
                raise SnapshotStateConflictError(
                    f"snapshot {snapshot_id} is not in a status that can be activated"
                )

            await self._admin_store.save(current)

            await self._append_admin_audit(
                session,
                context,
                status="applied",
                failure_code=None,
                before_hash=before_admin_hash,
                after_hash=target_admin_hash,
                json_patch=patch,
            )
            await self._append_snapshot_audit(
                session,
                context,
                snapshot_id=snapshot_id,
                action="activate",
                status="applied",
                failure_code=None,
                before_hash=expected_current_digest,
                after_hash=target_digest,
            )
            session.add(
                OutboxEvent(
                    aggregate_type="runtime_snapshot",
                    aggregate_id=str(snapshot_id),
                    event_type="runtime_snapshot.activated",
                    payload_json={
                        "snapshot_id": str(snapshot_id),
                        "digest": target_digest,
                        "parent_id": str(parent_id) if parent_id else None,
                    },
                )
            )
            await session.commit()
        except AuditWriteError as exc:
            await session.rollback()
            logger.error(
                "runtime snapshot audit append failed for %s", snapshot_id, exc_info=exc
            )
            raise HTTPException(
                status_code=500,
                detail="configuration change rolled back because the audit append failed",
                headers={"X-MAP-Error-Code": "AUDIT_WRITE_FAILED"},
            ) from exc
        except (SnapshotStateConflictError, SnapshotConcurrentModificationError) as exc:
            await session.rollback()
            failure_code = (
                "SNAPSHOT_CONCURRENT_MODIFICATION"
                if isinstance(exc, SnapshotConcurrentModificationError)
                else "SNAPSHOT_STATE_CONFLICT"
            )
            await self._append_snapshot_audit(
                session,
                context,
                snapshot_id=snapshot_id,
                action="activate",
                status="failed",
                failure_code=failure_code,
                before_hash=expected_current_digest,
                after_hash=None,
                error_message=str(exc),
            )
            await session.commit()
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"X-MAP-Error-Code": failure_code},
            ) from exc

        return current, result

    async def publish(
        self,
        session: AsyncSession,
        snapshot_id: uuid.UUID,
        context: MutationContext,
    ) -> RuntimeSnapshotRecord:
        record = await self._snapshots.get(snapshot_id)
        if record is None:
            raise _snapshot_not_found()
        try:
            updated = await self._snapshots.transition_status(
                snapshot_id, "draft", "published"
            )
        except SnapshotStateConflictError as exc:
            await self._append_snapshot_audit(
                session,
                context,
                snapshot_id=snapshot_id,
                action="publish",
                status="rejected",
                failure_code="SNAPSHOT_STATE_CONFLICT",
                before_hash=record.digest,
                after_hash=None,
                error_message=str(exc),
            )
            await session.commit()
            raise _snapshot_state_conflict(str(exc)) from exc

        await self._append_snapshot_audit(
            session,
            context,
            snapshot_id=snapshot_id,
            action="publish",
            status="applied",
            failure_code=None,
            before_hash=record.digest,
            after_hash=record.digest,
        )
        session.add(
            OutboxEvent(
                aggregate_type="runtime_snapshot",
                aggregate_id=str(snapshot_id),
                event_type="runtime_snapshot.published",
                payload_json={"snapshot_id": str(snapshot_id), "digest": record.digest},
            )
        )
        await session.commit()
        return updated

    async def activate(
        self,
        session: AsyncSession,
        snapshot_id: uuid.UUID,
        expected_current_digest: str | None,
        context: MutationContext,
    ) -> RuntimeSnapshotRecord:
        record = await self._snapshots.get(snapshot_id)
        if record is None:
            raise _snapshot_not_found()
        try:
            updated = await self._snapshots.activate(snapshot_id, expected_current_digest)
        except SnapshotConcurrentModificationError as exc:
            await self._snapshot_cas_audit(
                session, context, snapshot_id, "activate", record.digest, str(exc)
            )
            await session.commit()
            raise _snapshot_concurrent_modification(str(exc)) from exc
        except SnapshotStateConflictError as exc:
            await self._snapshot_cas_audit(
                session, context, snapshot_id, "activate", record.digest, str(exc)
            )
            await session.commit()
            raise _snapshot_state_conflict(str(exc)) from exc

        await self._append_snapshot_audit(
            session,
            context,
            snapshot_id=snapshot_id,
            action="activate",
            status="applied",
            failure_code=None,
            before_hash=expected_current_digest,
            after_hash=record.digest,
        )
        session.add(
            OutboxEvent(
                aggregate_type="runtime_snapshot",
                aggregate_id=str(snapshot_id),
                event_type="runtime_snapshot.activated",
                payload_json={
                    "snapshot_id": str(snapshot_id),
                    "digest": record.digest,
                    "previous_digest": expected_current_digest,
                },
            )
        )
        await session.commit()
        return updated

    async def rollback(
        self,
        session: AsyncSession,
        target_id: uuid.UUID,
        context: MutationContext,
    ) -> RuntimeSnapshotRecord:
        current = await self._snapshots.get_current()
        if current is None:
            raise _snapshot_state_conflict("no active snapshot to roll back")
        target = await self._snapshots.get(target_id)
        if target is None:
            raise _snapshot_not_found()
        try:
            updated = await self._snapshots.activate(target_id, current.digest)
        except SnapshotConcurrentModificationError as exc:
            await self._snapshot_cas_audit(
                session, context, target_id, "rollback", target.digest, str(exc)
            )
            await session.commit()
            raise _snapshot_concurrent_modification(str(exc)) from exc
        except SnapshotStateConflictError as exc:
            await self._snapshot_cas_audit(
                session, context, target_id, "rollback", target.digest, str(exc)
            )
            await session.commit()
            raise _snapshot_state_conflict(str(exc)) from exc

        await self._append_snapshot_audit(
            session,
            context,
            snapshot_id=target_id,
            action="rollback",
            status="applied",
            failure_code=None,
            before_hash=current.digest,
            after_hash=target.digest,
        )
        session.add(
            OutboxEvent(
                aggregate_type="runtime_snapshot",
                aggregate_id=str(target_id),
                event_type="runtime_snapshot.rollback",
                payload_json={
                    "snapshot_id": str(target_id),
                    "digest": target.digest,
                    "previous_digest": current.digest,
                },
            )
        )
        await session.commit()
        return updated

    async def retire(
        self,
        session: AsyncSession,
        snapshot_id: uuid.UUID,
        context: MutationContext,
    ) -> RuntimeSnapshotRecord:
        record = await self._snapshots.get(snapshot_id)
        if record is None:
            raise _snapshot_not_found()
        if record.status == "active":
            await self._append_snapshot_audit(
                session,
                context,
                snapshot_id=snapshot_id,
                action="retire",
                status="rejected",
                failure_code="SNAPSHOT_STATE_CONFLICT",
                before_hash=record.digest,
                after_hash=None,
            )
            await session.commit()
            raise _snapshot_state_conflict("active snapshot cannot be retired directly")
        try:
            updated = await self._snapshots.transition_status(
                snapshot_id, record.status, "retired"
            )
        except SnapshotStateConflictError as exc:
            await self._append_snapshot_audit(
                session,
                context,
                snapshot_id=snapshot_id,
                action="retire",
                status="rejected",
                failure_code="SNAPSHOT_STATE_CONFLICT",
                before_hash=record.digest,
                after_hash=None,
                error_message=str(exc),
            )
            await session.commit()
            raise _snapshot_state_conflict(str(exc)) from exc

        await self._append_snapshot_audit(
            session,
            context,
            snapshot_id=snapshot_id,
            action="retire",
            status="applied",
            failure_code=None,
            before_hash=record.digest,
            after_hash=record.digest,
        )
        session.add(
            OutboxEvent(
                aggregate_type="runtime_snapshot",
                aggregate_id=str(snapshot_id),
                event_type="runtime_snapshot.retired",
                payload_json={"snapshot_id": str(snapshot_id), "digest": record.digest},
            )
        )
        await session.commit()
        return updated

    async def get(self, snapshot_id: uuid.UUID) -> RuntimeSnapshotRecord | None:
        return await self._snapshots.get(snapshot_id)

    async def get_current(self) -> RuntimeSnapshotRecord | None:
        return await self._snapshots.get_current()

    async def _append_admin_audit(
        self,
        session: AsyncSession,
        context: MutationContext,
        *,
        status: str,
        failure_code: str | None,
        before_hash: str | None,
        after_hash: str | None,
        json_patch: list[dict] | None,
        error_message: str | None = None,
    ) -> None:
        await append_audit_event(
            session,
            workspace_id=_workspace_uuid(context.principal),
            resource_type=context.resource_type,
            resource_id=context.resource_id,
            action=context.action,
            actor_user_id=context.principal.user_id,
            actor_subject=context.principal.subject,
            actor_roles=list(context.principal.roles),
            request_id=_request_id(context.request),
            status=status,
            failure_code=failure_code,
            before_hash=before_hash,
            after_hash=after_hash,
            json_patch=json_patch,
            recovered=False,
            error_message=error_message,
            source_ip=(context.request.client.host if context.request.client else None),
            user_agent=(
                context.request.headers.get("User-Agent")[:2000]
                if context.request.headers.get("User-Agent")
                else None
            ),
        )

    async def _append_snapshot_audit(
        self,
        session: AsyncSession,
        context: MutationContext,
        *,
        snapshot_id: uuid.UUID,
        action: str,
        status: str,
        failure_code: str | None,
        before_hash: str | None,
        after_hash: str | None,
        error_message: str | None = None,
    ) -> None:
        await append_audit_event(
            session,
            workspace_id=_workspace_uuid(context.principal),
            resource_type="runtime_snapshot",
            resource_id=str(snapshot_id),
            action=action,
            actor_user_id=context.principal.user_id,
            actor_subject=context.principal.subject,
            actor_roles=list(context.principal.roles),
            request_id=_request_id(context.request),
            status=status,
            failure_code=failure_code,
            before_hash=before_hash,
            after_hash=after_hash,
            json_patch=None,
            recovered=False,
            error_message=error_message,
            source_ip=(context.request.client.host if context.request.client else None),
            user_agent=(
                context.request.headers.get("User-Agent")[:2000]
                if context.request.headers.get("User-Agent")
                else None
            ),
        )

    async def _snapshot_cas_audit(
        self,
        session: AsyncSession,
        context: MutationContext,
        snapshot_id: uuid.UUID,
        action: str,
        digest: str,
        error_message: str,
    ) -> None:
        await self._append_snapshot_audit(
            session,
            context,
            snapshot_id=snapshot_id,
            action=action,
            status="rejected",
            failure_code="SNAPSHOT_CONCURRENT_MODIFICATION",
            before_hash=digest,
            after_hash=None,
            error_message=error_message,
        )

def _snapshot_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="runtime config snapshot not found",
        headers={"X-MAP-Error-Code": "SNAPSHOT_NOT_FOUND"},
    )


def _snapshot_state_conflict(detail: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=detail,
        headers={"X-MAP-Error-Code": "SNAPSHOT_STATE_CONFLICT"},
    )


def _snapshot_concurrent_modification(detail: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=detail,
        headers={"X-MAP-Error-Code": "SNAPSHOT_CONCURRENT_MODIFICATION"},
    )
