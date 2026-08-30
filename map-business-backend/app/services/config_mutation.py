"""Hash-chained config audit append helpers (R1-AUDIT-01 / R2-P1-03).

These are the only surviving pieces of the old config mutation module after
Step 7 PR-J7b. AdminState writes now go through
``RuntimeSnapshotService.apply_change`` as ONE PostgreSQL transaction;
there is no pending mutation table and no file-store reconciler anymore.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConfigAuditChainHead, ConfigAuditEvent

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
