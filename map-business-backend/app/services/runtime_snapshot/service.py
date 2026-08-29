"""Runtime snapshot lifecycle service (Step 7 PR-J2).

``apply_change`` mirrors the crash semantics of
``ConfigMutationService.apply_mutation`` (config_mutation.py): load the
AdminState fail-closed -> prepare the target state/hash purely -> commit a
pending ``runtime_snapshot_mutations`` row -> CAS + file rename -> insert
draft snapshot -> publish -> activate -> audit + outbox -> finish pending.
An audit write failure is never swallowed: the request fails and the
pending row stays for the reconciler, which closes it only on exact hash
and pointer matches.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...core.identity import RequestPrincipal
from ...core.redaction import redact_payload
from ...db.models import OutboxEvent, RuntimeSnapshotMutation
from ...store import (
    AdminStateStore,
    BadStateFileError,
    ConcurrentModificationError,
    PreparedUpdate,
    StoreWriteError,
    state_hash,
)
from ..config_mutation import AuditWriteError, append_audit_event
from ..json_diff import json_patch_diff
from .digest import projection_digest, snapshot_id_for_digest
from .errors import SnapshotConcurrentModificationError, SnapshotStateConflictError
from .repository import RuntimeSnapshotRepository
from .schemas import (
    MutationContext,
    RuntimeProjection,
    RuntimeSnapshotRecord,
    build_runtime_projection,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AdminStateMutationStore(Protocol):
    """Narrow store protocol used by the snapshot service.

    The existing :class:`AdminStateStore` is an adapter; callers never see
    ``update(updater)`` and therefore cannot bypass the prepare/apply
    crash-recovery sequence.
    """

    def load(self) -> Any:
        """Return the current validated admin state."""
        ...

    def prepare_update(
        self, expected_hash: str, updater: Callable[[Any], T]
    ) -> PreparedUpdate[T]:
        """Pure computation phase (no write)."""
        ...

    def apply_prepared(self, prepared: PreparedUpdate[T]) -> None:
        """Apply phase: CAS + atomic file write."""
        ...


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
        admin_store: AdminStateMutationStore,
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
        """Audited snapshot write from an AdminState change.

        Crash recovery point: a pending ``runtime_snapshot_mutations`` row
        (with both admin hashes, the target snapshot id/digest/projection
        and the original request context) is committed BEFORE the file
        rename; the snapshot side and audit events are committed AFTER.
        """
        workspace_id = _workspace_uuid(context.principal)
        request_id = _request_id(context.request)

        # 1. Load current admin state (fail closed; never overwrite a
        #    corrupt file).
        try:
            current = self._admin_store.load()
        except BadStateFileError as exc:
            await self._append_admin_audit(
                session,
                context,
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
        before_admin_hash = state_hash(current)

        # 2. Prepare: pure computation (no file write, no pending row).
        try:
            prepared = self._admin_store.prepare_update(before_admin_hash, build_draft)
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
        except ConcurrentModificationError as exc:
            await self._append_admin_audit(
                session,
                context,
                status="rejected",
                failure_code="CONCURRENT_MODIFICATION",
                before_hash=before_admin_hash,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise HTTPException(
                status_code=409,
                detail="admin state changed concurrently; retry with the new state",
                headers={"X-MAP-Error-Code": "CONCURRENT_MODIFICATION"},
            ) from exc

        target_admin_hash = prepared.target_hash
        projection = build_runtime_projection(prepared.state)
        target_digest = projection_digest(projection)
        snapshot_id = snapshot_id_for_digest(target_digest)
        before_sanitized = redact_payload(current.model_dump())
        after_sanitized = redact_payload(prepared.state.model_dump())
        patch = json_patch_diff(before_sanitized, after_sanitized)

        current_snapshot = await self._snapshots.get_current()
        expected_current_digest = current_snapshot.digest if current_snapshot else None
        parent_id = current_snapshot.id if current_snapshot else None

        # 3. Crash recovery point: persist expected/target hashes + full
        #    request context BEFORE any file rename.
        mutation = RuntimeSnapshotMutation(
            resource=f"{context.resource_type}:{context.resource_id}",
            snapshot_id=snapshot_id,
            expected_admin_hash=before_admin_hash,
            target_admin_hash=target_admin_hash,
            expected_current_digest=expected_current_digest,
            target_current_digest=target_digest,
            target_projection=projection.model_dump(mode="json"),
            status="pending",
            workspace_id=workspace_id,
            action=context.action,
            actor_user_id=context.principal.user_id,
            actor_subject=context.principal.subject,
            actor_roles=list(context.principal.roles),
            request_id=request_id,
        )
        session.add(mutation)
        await session.commit()
        mutation_id = mutation.id

        # 4. Apply: CAS + atomic file rename.
        try:
            self._admin_store.apply_prepared(prepared)
        except ConcurrentModificationError as exc:
            await self._fail_mutation(
                session, mutation_id, error="concurrent modification between prepare and apply"
            )
            await self._append_admin_audit(
                session,
                context,
                status="rejected",
                failure_code="CONCURRENT_MODIFICATION",
                before_hash=before_admin_hash,
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
            await self._fail_mutation(
                session, mutation_id, error="store write failed"
            )
            await self._append_admin_audit(
                session,
                context,
                status="failed",
                failure_code="STORE_WRITE_FAILED",
                before_hash=before_admin_hash,
                after_hash=None,
                json_patch=None,
            )
            await session.commit()
            raise HTTPException(
                status_code=500,
                detail="admin state write failed; previous file kept intact",
                headers={"X-MAP-Error-Code": "STORE_WRITE_FAILED"},
            ) from exc

        # 5. Snapshot side + audit + outbox + finish pending (one
        #    transaction). Audit failure rolls this transaction back; the
        #    pending row committed in step 3 stays for the reconciler.
        try:
            inserted = await self._snapshots.insert(
                snapshot_id, projection, target_digest, parent_id, "draft"
            )
            if inserted.status == "draft":
                await self._snapshots.transition_status(snapshot_id, "draft", "published")
            if inserted.status in ("draft", "published", "active"):
                await self._snapshots.activate(snapshot_id, expected_current_digest)
            else:
                raise SnapshotStateConflictError(
                    f"snapshot {snapshot_id} is not in a status that can be activated"
                )

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
            await self._finish_mutation(session, mutation_id, status="applied")
            await session.commit()
        except AuditWriteError as exc:
            await session.rollback()
            logger.error(
                "runtime snapshot audit append failed for %s", snapshot_id, exc_info=exc
            )
            raise HTTPException(
                status_code=500,
                detail="configuration saved but audit failed; reconciled on next startup",
                headers={"X-MAP-Error-Code": "AUDIT_WRITE_FAILED"},
            ) from exc
        except (SnapshotStateConflictError, SnapshotConcurrentModificationError) as exc:
            await session.rollback()
            failure_code = (
                "SNAPSHOT_CONCURRENT_MODIFICATION"
                if isinstance(exc, SnapshotConcurrentModificationError)
                else "SNAPSHOT_STATE_CONFLICT"
            )
            await self._fail_mutation(session, mutation_id, error=str(exc))
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

        return prepared.state, prepared.result

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

    async def _finish_mutation(
        self,
        session: AsyncSession,
        mutation_id: uuid.UUID,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        await session.execute(
            update(RuntimeSnapshotMutation)
            .where(RuntimeSnapshotMutation.id == mutation_id)
            .values(status=status, error=error, finished_at=datetime.now(UTC))
        )

    async def _fail_mutation(
        self,
        session: AsyncSession,
        mutation_id: uuid.UUID,
        *,
        error: str | None = None,
    ) -> None:
        await self._finish_mutation(
            session, mutation_id, status="failed", error=error
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


async def reconcile_runtime_snapshot_mutations(
    session_factory: async_sessionmaker[AsyncSession],
    admin_store: AdminStateStore,
    snapshots_factory: Callable[[AsyncSession], RuntimeSnapshotRepository],
) -> int:
    """Crash recovery for pending runtime snapshot mutations.

    Exact-match rules only (mirrors ``reconcile_config_mutations``):

    - unreadable state file            -> failed / BAD_STATE_FILE
    - ``admin_hash == expected``       -> failed / NO_WRITE (no file write)
    - ``admin_hash == target`` and pointer still at ``expected_current_digest``
      -> applied: idempotently insert snapshot + activate
    - ``admin_hash == target`` and pointer already at ``target_current_digest``
      -> applied: snapshot side landed before the crash
    - anything else                    -> failed / UNKNOWN_STATE

    Every recovery writes a recovered audit event; attribution comes from
    the original mutation row (actor/request/action) when persisted.
    """
    async with session_factory() as session:
        pending = (
            (
                await session.execute(
                    select(RuntimeSnapshotMutation).where(
                        RuntimeSnapshotMutation.status == "pending"
                    )
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            return 0

        bad_file: str | None = None
        try:
            current_admin_hash = state_hash(admin_store.load())
        except BadStateFileError as exc:
            current_admin_hash = None
            bad_file = str(exc)

        repo = snapshots_factory(session)
        current_snapshot = await repo.get_current()
        pointer_digest = current_snapshot.digest if current_snapshot else None

        recovered = 0
        for mutation in pending:
            if current_admin_hash is None:
                status = "failed"
                failure_code = "BAD_STATE_FILE"
            elif current_admin_hash == mutation.expected_admin_hash:
                status = "failed"
                failure_code = "NO_WRITE"
            elif current_admin_hash == mutation.target_admin_hash:
                if pointer_digest == mutation.expected_current_digest:
                    try:
                        await _apply_snapshot_side(
                            repo, mutation, current_snapshot
                        )
                        status = "applied"
                        failure_code = None
                    except (
                        SnapshotStateConflictError,
                        SnapshotConcurrentModificationError,
                    ) as exc:
                        logger.warning(
                            "runtime snapshot reconciler could not apply %s: %s",
                            mutation.snapshot_id,
                            exc,
                        )
                        status = "failed"
                        failure_code = "UNKNOWN_STATE"
                elif pointer_digest == mutation.target_current_digest:
                    status = "applied"
                    failure_code = None
                else:
                    status = "failed"
                    failure_code = "UNKNOWN_STATE"
            else:
                status = "failed"
                failure_code = "UNKNOWN_STATE"

            await session.execute(
                update(RuntimeSnapshotMutation)
                .where(RuntimeSnapshotMutation.id == mutation.id)
                .values(status=status, finished_at=datetime.now(UTC))
            )
            await session.flush()
            await append_audit_event(
                session,
                workspace_id=mutation.workspace_id,
                resource_type="runtime_snapshot",
                resource_id=str(mutation.snapshot_id),
                action=mutation.action or "reconcile",
                actor_user_id=mutation.actor_user_id or "system:reconciler",
                actor_subject=mutation.actor_subject or "system:reconciler",
                actor_roles=list(mutation.actor_roles or []),
                request_id=mutation.request_id,
                status=status,
                failure_code=failure_code,
                before_hash=mutation.expected_current_digest,
                after_hash=(
                    mutation.target_current_digest
                    if status == "applied"
                    else pointer_digest
                ),
                json_patch=None,
                recovered=True,
                error_message=bad_file if current_admin_hash is None else None,
            )
            recovered += 1
        await session.commit()
    if recovered:
        logger.warning(
            "runtime snapshot reconciler recovered %d mutation(s)", recovered
        )
    return recovered


async def _apply_snapshot_side(
    repo: RuntimeSnapshotRepository,
    mutation: RuntimeSnapshotMutation,
    current_snapshot: RuntimeSnapshotRecord | None,
) -> None:
    projection = RuntimeProjection.model_validate(mutation.target_projection)
    parent_id = current_snapshot.id if current_snapshot else None
    record = await repo.insert(
        mutation.snapshot_id,
        projection,
        mutation.target_current_digest,
        parent_id,
        "draft",
    )
    if record.status == "draft":
        await repo.transition_status(mutation.snapshot_id, "draft", "published")
    await repo.activate(mutation.snapshot_id, mutation.expected_current_digest)
