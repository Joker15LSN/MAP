"""BFF-facing interface of the Canonical Run module.

This is the ONLY run surface the BFF may use. The implementation hides:
idempotency records, the 1:1 jobs linkage, SKIP LOCKED claims, lease
fencing, sequence assignment, terminal CAS and SSE projection.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from ..runtime.event_envelope import EventEnvelope
from ..runtime.state_machine import is_terminal
from .domain import (
    CancelReceipt,
    RunCommand,
    RunCreated,
    RunView,
)
from .errors import RunNotFoundError, RunTerminalStateError
from .store import RunStore


class RunApplication:
    def __init__(self, store: RunStore) -> None:
        self._store = store

    async def create_run(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        conversation_id: uuid.UUID | None,
        command: RunCommand,
        runtime_snapshot_id: uuid.UUID,
        runtime_snapshot_digest: str,
        idempotency_key: str,
        idempotency_body_hash: str,
    ) -> RunCreated:
        result = await self._store.create_run(
            workspace_id=workspace_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
            command=command,
            runtime_snapshot_id=runtime_snapshot_id,
            runtime_snapshot_digest=runtime_snapshot_digest,
            idempotency_key=idempotency_key,
            idempotency_body_hash=idempotency_body_hash,
        )
        return result.created

    async def get_run(
        self, *, workspace_id: uuid.UUID, principal_id: str, run_id: uuid.UUID
    ) -> RunView:
        view = await self._store.get_run_view(
            workspace_id=workspace_id, principal_id=principal_id, run_id=run_id
        )
        if view is None:
            raise RunNotFoundError(str(run_id))
        return view

    async def cancel_run(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        principal_id: str,
        reason: str = "",
    ) -> CancelReceipt:
        receipt = await self._store.submit_cancel_command(
            workspace_id=workspace_id,
            principal_id=principal_id,
            run_id=run_id,
            reason=reason,
        )
        if receipt is None:
            raise RunNotFoundError(str(run_id))
        if not receipt.accepted and is_terminal("run", receipt.status):
            raise RunTerminalStateError(str(run_id), receipt.status)
        return receipt

    async def replay_events(
        self,
        *,
        workspace_id: uuid.UUID,
        principal_id: str,
        run_id: uuid.UUID,
        after_seq: int = 0,
    ) -> AsyncIterator[EventEnvelope]:
        # Ownership check first so a missing/cross-workspace run is a stable
        # 404 before any stream starts.
        await self.get_run(
            workspace_id=workspace_id, principal_id=principal_id, run_id=run_id
        )
        async for envelope in self._store.read_events_after(
            workspace_id=workspace_id,
            principal_id=principal_id,
            run_id=run_id,
            after_seq=after_seq,
        ):
            yield envelope
