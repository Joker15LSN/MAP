"""S4-01: SandboxInvocation ledger semantics (in-memory + PostgreSQL).

The in-memory double and the PostgreSQL implementation must share the SAME
claim semantics: unique (workspace_id, invocation_id), digest conflict,
terminal replay, in-progress for a non-terminal owner.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from map_core.service.sandbox_ledger import (
    STATUS_SUCCEEDED,
    InMemorySandboxInvocationLedger,
    PostgresSandboxInvocationLedger,
    SandboxLedgerError,
    build_create_key,
    build_execute_key,
    normalize_request_digest,
)

LIMITS = {
    "cpu_seconds": 30,
    "memory_mb": 512,
    "disk_mb": 1024,
    "max_output_bytes": 65536,
    "timeout_seconds": 30,
}


def _digest(command: str) -> str:
    return normalize_request_digest(command=command, limits=LIMITS)


def _claim_kwargs(workspace_id, invocation_id, command="echo hi"):
    digest = _digest(command)
    return {
        "workspace_id": workspace_id,
        "invocation_id": invocation_id,
        "request_digest": digest,
        "create_key": build_create_key(
            workspace_id=workspace_id, invocation_id=invocation_id, request_digest=digest
        ),
        "execute_key": build_execute_key(
            workspace_id=workspace_id, invocation_id=invocation_id, request_digest=digest
        ),
    }


async def _exercise(ledger) -> None:
    # First caller owns.
    first = await ledger.claim(**_claim_kwargs("ws-a", "inv-1"))
    assert first.kind == "owned"
    assert first.record is not None and first.record.status == "pending"

    # Second caller (same identity, non-terminal) observes in-progress.
    second = await ledger.claim(**_claim_kwargs("ws-a", "inv-1"))
    assert second.kind == "in_progress"

    # Different workspace, same invocation: independent claim.
    other = await ledger.claim(**_claim_kwargs("ws-b", "inv-1"))
    assert other.kind == "owned"
    assert other.record.create_key != first.record.create_key

    # Same identity, different digest: conflict.
    conflict = await ledger.claim(**_claim_kwargs("ws-a", "inv-1", command="echo two"))
    assert conflict.kind == "conflict"

    # Settle the first invocation to succeeded, then replay.
    await ledger.record_created(workspace_id="ws-a", invocation_id="inv-1", sandbox_id="sb-1")
    await ledger.complete(
        workspace_id="ws-a",
        invocation_id="inv-1",
        status=STATUS_SUCCEEDED,
        sandbox_id="sb-1",
        output="ok",
    )
    replay = await ledger.claim(**_claim_kwargs("ws-a", "inv-1"))
    assert replay.kind == "replay"
    assert replay.record.output == "ok"
    assert replay.record.status == STATUS_SUCCEEDED

    # A different digest for a terminal row still conflicts.
    conflict_after = await ledger.claim(
        **_claim_kwargs("ws-a", "inv-1", command="echo three")
    )
    assert conflict_after.kind == "conflict"


def test_in_memory_ledger_semantics() -> None:
    ledger = InMemorySandboxInvocationLedger()
    asyncio.run(_exercise(ledger))


def test_in_memory_claim_atomic_under_concurrency() -> None:
    ledger = InMemorySandboxInvocationLedger()
    kwargs = _claim_kwargs("ws-c", "inv-c")

    async def run() -> list[str]:
        outcomes = await asyncio.gather(*(ledger.claim(**kwargs) for _ in range(200)))
        return [o.kind for o in outcomes]

    kinds = asyncio.run(run())
    assert kinds.count("owned") == 1
    assert kinds.count("in_progress") == 199


def test_postgres_ledger_semantics() -> None:
    dsn = os.getenv("POSTGRES_DSN", "postgresql://map:map@127.0.0.1:15432/map")
    suffix = uuid4().hex[:8]
    ledger = PostgresSandboxInvocationLedger(dsn)

    async def run() -> None:
        ws = f"ws-{suffix}"
        inv = f"inv-{suffix}"
        try:
            first = await ledger.claim(**_claim_kwargs(ws, inv))
        except SandboxLedgerError as exc:  # optional local PG / migration
            pytest.skip(f"postgres ledger unavailable: {exc}")
        assert first.kind == "owned"
        second = await ledger.claim(**_claim_kwargs(ws, inv))
        assert second.kind == "in_progress"
        conflict = await ledger.claim(**_claim_kwargs(ws, inv, command="echo other"))
        assert conflict.kind == "conflict"
        await ledger.record_created(workspace_id=ws, invocation_id=inv, sandbox_id="sb-x")
        await ledger.complete(
            workspace_id=ws, invocation_id=inv, status=STATUS_SUCCEEDED, output="ok"
        )
        replay = await ledger.claim(**_claim_kwargs(ws, inv))
        assert replay.kind == "replay"
        assert replay.record is not None and replay.record.output == "ok"
        await ledger.close()

    asyncio.run(run())
