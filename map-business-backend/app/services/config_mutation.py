"""Unified config mutation service (R1-AUDIT-01 / FIX-P1-AUDIT-01).

Every AdminState write goes through :func:`apply_mutation`, split into
prepare + apply (R3-P1-01):

1. pure computation: prepare the target state/hash and the redacted
   patch in memory — NO file is touched;
2. short transaction: insert a pending ``config_mutations`` row carrying
   ``expected_hash`` + ``target_hash`` + the original request context
   (workspace/actor/request/resource/action) and commit — the crash
   recovery point, BEFORE any rename;
3. expected-hash CAS + atomic file rename;
4. short transaction: append the applied audit event (redacted JSON
   Patch, hash-chained) and finish the mutation.

An audit write failure is NEVER swallowed: the request fails and the
mutation stays pending for the reconciler, which closes a pending row
only on an exact hash match — ``current == expected`` means no write
happened (NO_WRITE), ``current == target`` means the write landed
(applied, ``after_hash`` exactly ``target_hash``); every other hash is
``UNKNOWN_STATE`` and never a guessed ``applied``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from fastapi import HTTPException, Request
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.identity import RequestPrincipal
from ..core.redaction import redact_payload
from ..db.models import ConfigAuditChainHead, ConfigAuditEvent, ConfigMutation
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

# Explicit split of ``config_audit_events`` columns (R2-P1-03): every
# hash-relevant field lives in the canonical record persisted on the row
# (see ``audit_record_payload``); everything else is display/forensics only
# and may never influence ``entry_hash``.
NON_HASH_RELEVANT_COLUMNS = frozenset(
    {
        "id",
        "source_ip",
        "user_agent",
        "before_version",
        "after_version",
        "created_at",
        # chain position fields: integrity-checked structurally by the
        # verifier (ordinal walk + prev link), never mixed into the record
        # payload:
        "ordinal",
        "prev_entry_hash",
        "entry_hash",
    }
)

_ERROR_MESSAGE_LIMIT = 2000


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


async def append_audit_event(
    session: AsyncSession,
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
    recovered: bool = False,
    error_message: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> ConfigAuditEvent:
    """Append one hash-chained audit event in the caller's transaction.

    Concurrency (R2-P1-03): all appends serialize on the single
    ``config_audit_chain_head`` row (``SELECT ... FOR UPDATE``); the head
    advance and the event insert commit atomically, so the chain can never
    fork. ``UNIQUE(prev_entry_hash)``/``UNIQUE(ordinal)`` back this up at
    the database level. Every hash-canonical field — including
    ``error_message`` — is persisted so the verifier recomputes from one
    schema only.
    """
    head = await _lock_chain_head(session)
    ordinal = int(head.head_ordinal)
    prev_hash = head.head_entry_hash or ""
    if error_message is not None:
        error_message = error_message[:_ERROR_MESSAGE_LIMIT]
    record = audit_record_payload(
        workspace_id=workspace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        actor_user_id=actor_user_id,
        actor_subject=actor_subject,
        actor_roles=list(actor_roles),
        request_id=request_id,
        status=status,
        failure_code=failure_code,
        before_hash=before_hash,
        after_hash=after_hash,
        json_patch=json_patch,
        recovered=recovered,
        error_message=error_message,
    )
    entry_hash = compute_entry_hash(prev_hash, record)
    event = ConfigAuditEvent(
        workspace_id=workspace_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        actor_user_id=actor_user_id,
        actor_subject=actor_subject,
        actor_roles=list(actor_roles),
        request_id=request_id,
        source_ip=source_ip,
        user_agent=user_agent,
        before_version=before_hash[:16] if before_hash else None,
        after_version=after_hash[:16] if after_hash else None,
        json_patch=json_patch,
        before_hash=before_hash,
        after_hash=after_hash,
        status=status,
        failure_code=failure_code,
        recovered=recovered,
        prev_entry_hash=prev_hash,
        entry_hash=entry_hash,
        ordinal=ordinal,
        error_message=error_message,
    )
    session.add(event)
    head.head_ordinal = ordinal + 1
    head.head_entry_hash = entry_hash
    head.updated_at = datetime.now(UTC)
    try:
        await session.flush()
    except Exception as exc:  # audit must fail loudly
        raise AuditWriteError(str(exc)) from exc
    return event


async def _lock_chain_head(session: AsyncSession) -> ConfigAuditChainHead:
    """Seed (idempotent) and row-lock the global chain head."""
    # Re-seed after TRUNCATE/first use; ON CONFLICT keeps this race-free.
    await session.execute(
        text(
            "INSERT INTO map_control.config_audit_chain_head "
            "(chain_id, head_ordinal, head_entry_hash) "
            "VALUES (1, 0, '') ON CONFLICT (chain_id) DO NOTHING"
        )
    )
    head = (
        (
            await session.execute(
                select(ConfigAuditChainHead)
                .where(ConfigAuditChainHead.chain_id == 1)
                .with_for_update()
            )
        )
        .scalars()
        .one_or_none()
    )
    if head is None:  # pragma: no cover - defensive
        raise AuditWriteError("audit chain head row is missing")
    return head


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
        request_id = getattr(request.state, "request_id", None)

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
                error_message=str(exc),
            )
            await session.commit()
            raise HTTPException(
                status_code=500,
                detail=f"admin state file is corrupt and kept untouched: {exc}",
                headers={"X-MAP-Error-Code": "BAD_STATE_FILE"},
            ) from exc
        before_hash = state_hash(current)

        # 2. Prepare: pure computation of the target state/hash/patch.
        # Nothing is written yet, so rejections below need no pending row.
        try:
            prepared = self._store.prepare_update(before_hash, updater)
        except HTTPException as exc:
            # Business rejection raised by the updater (404/409, ...): no
            # state change happened, but the attempt is still audited.
            failure_code = "BUSINESS_REJECTED"
            if getattr(exc, "headers", None) and exc.headers.get("X-MAP-Error-Code"):
                failure_code = exc.headers["X-MAP-Error-Code"]
            await self._append_event(
                session,
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                principal=principal,
                request=request,
                status="rejected",
                failure_code=failure_code,
                before_hash=before_hash,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise
        except ConcurrentModificationError as exc:
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

        target_hash = prepared.target_hash
        before_sanitized = redact_payload(current.model_dump())
        after_sanitized = redact_payload(prepared.state.model_dump())
        patch = json_patch_diff(before_sanitized, after_sanitized)

        # 3. Crash recovery point: expected_hash + target_hash + the full
        # request context are persisted BEFORE any rename happens.
        mutation = ConfigMutation(
            resource=f"{resource_type}:{resource_id}",
            expected_hash=before_hash,
            target_hash=target_hash,
            status="pending",
            workspace_id=workspace_id,
            action=action,
            actor_user_id=principal.user_id,
            actor_subject=principal.subject,
            actor_roles=list(principal.roles),
            request_id=request_id,
        )
        session.add(mutation)
        await session.commit()

        # 4. Apply: expected-hash CAS + atomic rename.
        try:
            self._store.apply_prepared(prepared)
        except ConcurrentModificationError as exc:
            await self._finish_mutation(
                session,
                mutation,
                status="failed",
                error="concurrent modification between prepare and apply",
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

        # 5. Audit event + mutation terminal state (same transaction).
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
                after_hash=target_hash,
                json_patch=patch,
            )
            await self._finish_mutation(session, mutation, status="applied")
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
        return prepared.state, prepared.result

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
        """Append one hash-chained event from an HTTP request context."""
        return await append_audit_event(
            session,
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
            source_ip=(request.client.host if request.client else None),
            user_agent=request.headers.get("User-Agent")[:2000]
            if request.headers.get("User-Agent")
            else None,
        )

    async def _finish_mutation(
        self,
        session: AsyncSession,
        mutation: ConfigMutation,
        *,
        status: str,
        error: str | None = None,
        target_hash: str | None = None,
    ) -> None:
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

    Exact-match rules only (R3-P1-01) — the pending row was committed
    BEFORE the rename with ``expected_hash`` + ``target_hash``:

    - unreadable state file          -> failed / BAD_STATE_FILE
    - ``current == expected_hash``   -> the rename never happened:
                                        failed / NO_WRITE
    - ``current == target_hash``     -> the rename landed: applied
      (``after_hash`` exactly ``target_hash``)
    - anything else                  -> failed / UNKNOWN_STATE: someone
      else's write changed the file; never a guessed ``applied``.

    Idempotent: only pending mutations are touched. Recovered events keep
    the mutation's original workspace/actor/request/resource/action when
    those were persisted, so attribution survives the crash.
    """
    async with session_factory() as session:
        pending = (
            (
                await session.execute(
                    select(ConfigMutation).where(ConfigMutation.status == "pending")
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            return 0
        bad_file: str | None = None
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
            elif mutation.target_hash and current_hash == mutation.target_hash:
                status = "applied"
                failure_code = None
            else:
                # Any other hash — including legacy rows without a
                # target_hash — is indistinguishable from an unrelated
                # write: report it, never guess.
                status = "failed"
                failure_code = "UNKNOWN_STATE"
            await session.execute(
                update(ConfigMutation)
                .where(ConfigMutation.id == mutation.id)
                .values(status=status, finished_at=func_now())
            )
            await session.flush()
            context = _recovery_context(mutation)
            await append_audit_event(
                session,
                workspace_id=context.workspace_id,
                resource_type=resource_type or "config",
                resource_id=resource_id,
                action=context.action,
                actor_user_id=context.actor_user_id,
                actor_subject=context.actor_subject,
                actor_roles=context.actor_roles,
                request_id=context.request_id,
                status=status,
                failure_code=failure_code,
                before_hash=mutation.expected_hash,
                # An applied recovery landed EXACTLY the persisted target;
                # anything else records the observed (foreign) hash.
                after_hash=mutation.target_hash if status == "applied" else current_hash,
                json_patch=None,
                recovered=True,
                error_message=bad_file if current_hash is None else None,
            )
            recovered += 1
        await session.commit()
    if recovered:
        logger.warning("config mutation reconciler recovered %d mutation(s)", recovered)
    return recovered


def func_now():
    return datetime.now(UTC)


@dataclass(frozen=True)
class _RecoveryContext:
    """Attribution of a recovered audit event: the original request's
    identity when it was persisted on the mutation row, a synthetic
    reconciler identity otherwise."""

    workspace_id: Any
    action: str
    actor_user_id: str
    actor_subject: str | None
    actor_roles: list[str]
    request_id: str | None


def _recovery_context(mutation: ConfigMutation) -> _RecoveryContext:
    return _RecoveryContext(
        workspace_id=mutation.workspace_id,
        action=mutation.action or "reconcile",
        actor_user_id=mutation.actor_user_id or "system:reconciler",
        actor_subject=mutation.actor_subject or "system:reconciler",
        actor_roles=list(mutation.actor_roles or []),
        request_id=mutation.request_id,
    )
