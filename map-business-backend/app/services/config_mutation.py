"""Unified config mutation service (R1-AUDIT-01 / FIX-P1-AUDIT-01).

Every AdminState write goes through :func:`apply_mutation`:

1. short transaction: insert a pending ``config_mutations`` row (expected
   hash) and commit — the crash recovery point;
2. optimistic atomic file write (expected-hash check, temp file + fsync +
   rename);
3. short transaction: append the applied/failed/rejected audit event
   (redacted JSON Patch, hash-chained) and finish the mutation.

An audit write failure is NEVER swallowed: the request fails and the
mutation stays pending for the reconciler (which appends a ``recovered``
event based on before/target/current hashes).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any, TypeVar

from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.identity import RequestPrincipal
from ..core.redaction import redact_payload
from ..db.models import ConfigAuditEvent, ConfigMutation
from ..store import (
    AdminStateStore,
    BadStateFileError,
    ConcurrentModificationError,
    StoreWriteError,
    state_hash,
)
from .json_diff import json_patch_diff

logger = logging.getLogger(__name__)

T = TypeVar("T")


def audit_record_payload(
    *,
    workspace_id: Any,
    resource_type: str,
    resource_id: str,
    action: str,
    actor_user_id: str,
    actor_subject: str | None,
    actor_roles: list[str],
    request_id: str | None,
    status: str,
    failure_code: str | None,
    before_hash: str | None,
    after_hash: str | None,
    json_patch: list[dict] | None,
    recovered: bool,
    error_message: str | None,
) -> dict:
    """The canonical, hash-relevant record fields (shared by writer/verifier)."""
    return {
        "workspace_id": str(workspace_id) if workspace_id else None,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "action": action,
        "actor_user_id": actor_user_id,
        "actor_subject": actor_subject,
        "actor_roles": list(actor_roles),
        "request_id": request_id,
        "status": status,
        "failure_code": failure_code,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "json_patch": json_patch,
        "recovered": recovered,
        "error_message": error_message,
    }


def compute_entry_hash(prev_hash: str | None, record: dict) -> str:
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(((prev_hash or "") + canonical).encode("utf-8")).hexdigest()


class AuditWriteError(Exception):
    """Audit append failed; the product write may have succeeded already."""


class ConfigMutationService:
    def __init__(self, store: AdminStateStore) -> None:
        self._store = store

    async def apply_mutation(
        self,
        *,
        session: AsyncSession,
        request: Request,
        principal: RequestPrincipal,
        resource_type: str,
        resource_id: str,
        action: str,
        updater: Callable[[Any], T],
    ) -> tuple[Any, T]:
        """Audited optimistic write. Raises HTTPException on failure."""
        workspace_id = _workspace_uuid(principal)

        # 1. Load the current state (fail closed, keep the corrupt file).
        try:
            current = self._store.load()
        except BadStateFileError as exc:
            await self._append_event(
                session,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                principal=principal,
                request=request,
                status="failed",
                failure_code="BAD_STATE_FILE",
                before_hash=None,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise HTTPException(
                status_code=500,
                detail=f"admin state file is corrupt and kept untouched: {exc}",
                headers={"X-MAP-Error-Code": "BAD_STATE_FILE"},
            ) from exc
        before_hash = state_hash(current)

        # 2. Pending mutation (crash recovery point), committed immediately.
        mutation = ConfigMutation(
            resource=f"{resource_type}:{resource_id}",
            expected_hash=before_hash,
            status="pending",
        )
        session.add(mutation)
        await session.commit()

        # 3. Optimistic atomic write.
        try:
            new_state, result = self._store.update_with_hash(before_hash, updater)
        except ConcurrentModificationError as exc:
            await self._finish_mutation(
                session,
                mutation,
                status="failed",
                error="concurrent modification",
            )
            await self._append_event(
                session,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                principal=principal,
                request=request,
                status="rejected",
                failure_code="CONCURRENT_MODIFICATION",
                before_hash=before_hash,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise HTTPException(
                status_code=409,
                detail="admin state changed concurrently; retry with the new state",
                headers={"X-MAP-Error-Code": "CONCURRENT_MODIFICATION"},
            ) from exc
        except StoreWriteError as exc:
            await self._finish_mutation(
                session, mutation, status="failed", error="store write failed"
            )
            await self._append_event(
                session,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                principal=principal,
                request=request,
                status="failed",
                failure_code="STORE_WRITE_FAILED",
                before_hash=before_hash,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise HTTPException(
                status_code=500,
                detail="admin state write failed; previous file kept intact",
                headers={"X-MAP-Error-Code": "STORE_WRITE_FAILED"},
            ) from exc
        except BadStateFileError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"admin state file is corrupt: {exc}",
                headers={"X-MAP-Error-Code": "BAD_STATE_FILE"},
            ) from exc

        after_hash = state_hash(new_state)
        before_sanitized = redact_payload(current.model_dump())
        after_sanitized = redact_payload(new_state.model_dump())
        patch = json_patch_diff(before_sanitized, after_sanitized)

        # 4. Audit event + mutation terminal state (same transaction).
        try:
            await self._append_event(
                session,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                principal=principal,
                request=request,
                status="applied",
                failure_code=None,
                before_hash=before_hash,
                after_hash=after_hash,
                json_patch=patch,
            )
            await self._finish_mutation(
                session,
                mutation,
                status="applied",
                target_hash=after_hash,
            )
            await session.commit()
        except AuditWriteError as exc:
            # Never report success when the audit is missing; the mutation
            # stays pending for the reconciler.
            await session.rollback()
            logger.error("audit append failed for %s:%s", resource_type, resource_id, exc_info=exc)
            raise HTTPException(
                status_code=500,
                detail="configuration saved but audit failed; reconciled on next startup",
                headers={"X-MAP-Error-Code": "AUDIT_WRITE_FAILED"},
            ) from exc
        return new_state, result

    async def _append_event(
        self,
        session: AsyncSession,
        *,
        workspace_id: Any,
        resource_type: str,
        resource_id: str,
        action: str,
        principal: RequestPrincipal,
        request: Request,
        status: str,
        failure_code: str | None,
        before_hash: str | None,
        after_hash: str | None,
        json_patch: list[dict] | None,
        recovered: bool = False,
        error_message: str | None = None,
    ) -> ConfigAuditEvent:
        """Append one hash-chained event; locks the chain tail in this tx."""
        # Lock the chain tail so concurrent appends never fork the chain.
        tail = (
            await session.execute(
                select(ConfigAuditEvent.entry_hash)
                .order_by(ConfigAuditEvent.created_at.desc(), ConfigAuditEvent.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        record = audit_record_payload(
            workspace_id=workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            actor_user_id=principal.user_id,
            actor_subject=principal.subject,
            actor_roles=list(principal.roles),
            request_id=getattr(request.state, "request_id", None),
            status=status,
            failure_code=failure_code,
            before_hash=before_hash,
            after_hash=after_hash,
            json_patch=json_patch,
            recovered=recovered,
            error_message=error_message,
        )
        entry_hash = compute_entry_hash(tail, record)

        try:
            event = ConfigAuditEvent(
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                actor_user_id=principal.user_id,
                actor_subject=principal.subject,
                actor_roles=list(principal.roles),
                request_id=getattr(request.state, "request_id", None),
                source_ip=(request.client.host if request.client else None),
                user_agent=request.headers.get("User-Agent")[:2000]
                if request.headers.get("User-Agent")
                else None,
                before_version=before_hash[:16] if before_hash else None,
                after_version=after_hash[:16] if after_hash else None,
                json_patch=json_patch,
                before_hash=before_hash,
                after_hash=after_hash,
                status=status,
                failure_code=failure_code,
                recovered=recovered,
                prev_entry_hash=tail,
                entry_hash=entry_hash,
            )
            session.add(event)
            await session.flush()
        except Exception as exc:  # audit must fail loudly
            raise AuditWriteError(str(exc)) from exc
        return event

    async def _finish_mutation(
        self,
        session: AsyncSession,
        mutation: ConfigMutation,
        *,
        status: str,
        error: str | None = None,
        target_hash: str | None = None,
    ) -> None:
        from datetime import datetime

        mutation.status = status
        mutation.error = error
        mutation.target_hash = target_hash or mutation.target_hash
        mutation.finished_at = datetime.now(UTC)
        await session.flush()


def _workspace_uuid(principal: RequestPrincipal) -> Any:
    import uuid

    try:
        return uuid.UUID(principal.workspace_id)
    except ValueError:
        return None


async def reconcile_config_mutations(
    session_factory: async_sessionmaker[AsyncSession],
    store: AdminStateStore,
) -> int:
    """Crash recovery: finish pending mutations with a ``recovered`` event.

    - current hash == expected hash  -> the write never happened (failed)
    - current hash != expected hash  -> the write happened (applied;
      ``target_hash`` verified when available)
    Idempotent: only pending mutations are touched.
    """
    async with session_factory() as session:
        pending = (
            await session.execute(
                select(ConfigMutation).where(ConfigMutation.status == "pending")
            )
        ).scalars().all()
        if not pending:
            return 0
        try:
            current_hash = state_hash(store.load())
        except BadStateFileError as exc:
            current_hash = None
            bad_file = str(exc)

        recovered = 0
        for mutation in pending:
            resource_type, _, resource_id = mutation.resource.partition(":")
            if current_hash is None:
                status = "failed"
                failure_code = "BAD_STATE_FILE"
            elif current_hash == mutation.expected_hash:
                status = "failed"
                failure_code = "NO_WRITE"
            elif mutation.target_hash and current_hash != mutation.target_hash:
                status = "failed"
                failure_code = "UNKNOWN_STATE"
            else:
                status = "applied"
                failure_code = None
            await session.execute(
                update(ConfigMutation)
                .where(ConfigMutation.id == mutation.id)
                .values(status=status, finished_at=func_now())
            )
            await session.flush()
            # Reconciler writes with a synthetic actor; recovered events keep
            # the mutation's identity in the patch-free record.
            principal = _RecoveryPrincipal(mutation.resource)
            await _recovery_append(
                session,
                store=store,
                resource_type=resource_type or "config",
                resource_id=resource_id,
                principal=principal,
                status=status,
                failure_code=failure_code,
                before_hash=mutation.expected_hash,
                after_hash=current_hash,
                recovered=True,
                error_message=bad_file if current_hash is None else None,
            )
            recovered += 1
        await session.commit()
    if recovered:
        logger.warning("config mutation reconciler recovered %d mutation(s)", recovered)
    return recovered


def func_now():
    from datetime import datetime

    return datetime.now(UTC)


async def _recovery_append(
    session: AsyncSession,
    *,
    store: AdminStateStore,
    resource_type: str,
    resource_id: str,
    principal: Any,
    status: str,
    failure_code: str | None,
    before_hash: str | None,
    after_hash: str | None,
    recovered: bool,
    error_message: str | None,
) -> None:
    """Reconciler audit append (no HTTP request context available)."""
    tail = (
        await session.execute(
            select(ConfigAuditEvent.entry_hash)
            .order_by(ConfigAuditEvent.created_at.desc(), ConfigAuditEvent.id.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    record = audit_record_payload(
        workspace_id=None,
        resource_type=resource_type,
        resource_id=resource_id,
        action="reconcile",
        actor_user_id=principal.user_id,
        actor_subject=principal.subject,
        actor_roles=[],
        request_id=None,
        status=status,
        failure_code=failure_code,
        before_hash=before_hash,
        after_hash=after_hash,
        json_patch=None,
        recovered=recovered,
        error_message=error_message,
    )
    entry_hash = compute_entry_hash(tail, record)
    session.add(
        ConfigAuditEvent(
            resource_type=resource_type,
            resource_id=resource_id,
            action="reconcile",
            actor_user_id=principal.user_id,
            actor_subject=principal.subject,
            actor_roles=[],
            before_version=before_hash[:16] if before_hash else None,
            after_version=after_hash[:16] if after_hash else None,
            before_hash=before_hash,
            after_hash=after_hash,
            status=status,
            failure_code=failure_code,
            recovered=recovered,
            prev_entry_hash=tail,
            entry_hash=entry_hash,
        )
    )
    await session.flush()


class _RecoveryPrincipal:
    def __init__(self, resource: str) -> None:
        self.user_id = "system:reconciler"
        self.subject = "system:reconciler"
        self.roles: tuple[str, ...] = ()
