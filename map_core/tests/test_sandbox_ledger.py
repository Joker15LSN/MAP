"""S5-01: SandboxInvocation ledger fencing semantics (in-memory + PG).

The in-memory double and the PostgreSQL implementation must share the SAME
claim/takeover semantics: unique (workspace_id, invocation_id), digest
conflict, terminal replay, in-progress for a LIVE lease, and an atomic
CAS takeover (new token + attempt bump) for an EXPIRED non-terminal row.
Fenced writes (record_created/complete) must match the caller's generation
or fail with SandboxOwnershipLost; renew heartbeats only the owner's
generation.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from map_core.service.sandbox_ledger import (
    STATUS_CREATED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    InMemorySandboxInvocationLedger,
    PostgresSandboxInvocationLedger,
    SandboxLedgerError,
    SandboxOwnershipLost,
    build_create_key,
    build_execute_key,
    new_fence,
    normalize_request_digest,
)

LIMITS = {
    "cpu_seconds": 30,
    "memory_mb": 512,
    "disk_mb": 1024,
    "max_output_bytes": 65536,
    "timeout_seconds": 30,
}

LEASE = 1.0


def _digest(command: str) -> str:
    return normalize_request_digest(command=command, limits=LIMITS)


def _claim_kwargs(
    workspace_id,
    invocation_id,
    command="echo hi",
    *,
    owner_id="owner-a",
    lease_seconds=LEASE,
):
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
        "request_payload": {"command": command, "limits": LIMITS},
        "owner_id": owner_id,
        "lease_seconds": lease_seconds,
    }


async def _exercise(ledger, *, sleep) -> None:
    # First caller owns.
    first = await ledger.claim(**_claim_kwargs("ws-a", "inv-1"))
    assert first.kind == "owned"
    assert first.fence is not None
    assert first.record is not None and first.record.status == "pending"
    assert first.record.attempt == 0

    # Second caller (same identity, LIVE lease) observes in-progress.
    second = await ledger.claim(**_claim_kwargs("ws-a", "inv-1", owner_id="owner-b"))
    assert second.kind == "in_progress"

    # Different workspace, same invocation: independent claim.
    other = await ledger.claim(**_claim_kwargs("ws-b", "inv-1"))
    assert other.kind == "owned"
    assert other.record.create_key != first.record.create_key

    # Same identity, different digest: conflict.
    conflict = await ledger.claim(**_claim_kwargs("ws-a", "inv-1", command="echo two"))
    assert conflict.kind == "conflict"

    # Settle the first invocation to succeeded, then replay.
    await ledger.record_created(
        workspace_id="ws-a",
        invocation_id="inv-1",
        sandbox_id="sb-1",
        fence=first.fence,
    )
    await ledger.complete(
        workspace_id="ws-a",
        invocation_id="inv-1",
        status=STATUS_SUCCEEDED,
        sandbox_id="sb-1",
        output="ok",
        fence=first.fence,
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

    async def run() -> None:
        await _exercise(ledger, sleep=asyncio.sleep)

    asyncio.run(run())


def test_in_memory_claim_atomic_under_concurrency() -> None:
    ledger = InMemorySandboxInvocationLedger()
    kwargs = _claim_kwargs("ws-c", "inv-c", lease_seconds=30.0)

    async def run() -> list[str]:
        outcomes = await asyncio.gather(*(ledger.claim(**kwargs) for _ in range(200)))
        return [o.kind for o in outcomes]

    kinds = asyncio.run(run())
    assert kinds.count("owned") == 1
    assert kinds.count("in_progress") == 199


def test_in_memory_takeover_after_lease_expiry() -> None:
    """The S5-01 counter-example: owner dies, a retry takes over and the row
    converges instead of hanging in pending forever."""
    ledger = InMemorySandboxInvocationLedger()

    async def run() -> None:
        first = await ledger.claim(**_claim_kwargs("ws-1", "inv-1", owner_id="owner-a"))
        assert first.kind == "owned"
        # Owner crash: the lease expires with no heartbeat and no writes.
        await asyncio.sleep(LEASE + 0.05)
        # A retry with the same digest takes over (never in_progress forever).
        second = await ledger.claim(**_claim_kwargs("ws-1", "inv-1", owner_id="owner-b"))
        assert second.kind == "takeover"
        assert second.fence is not None
        assert second.fence.attempt == 1
        assert second.record.owner_id == "owner-b"
        # The superseded owner can no longer write anything.
        with pytest.raises(SandboxOwnershipLost):
            await ledger.record_created(
                workspace_id="ws-1",
                invocation_id="inv-1",
                sandbox_id="sb-x",
                fence=first.fence,
            )
        with pytest.raises(SandboxOwnershipLost):
            await ledger.complete(
                workspace_id="ws-1",
                invocation_id="inv-1",
                status=STATUS_SUCCEEDED,
                fence=first.fence,
            )
        # The new owner converges the row to a definite terminal state.
        await ledger.record_created(
            workspace_id="ws-1", invocation_id="inv-1", sandbox_id="sb-x", fence=second.fence
        )
        await ledger.complete(
            workspace_id="ws-1",
            invocation_id="inv-1",
            status=STATUS_SUCCEEDED,
            sandbox_id="sb-x",
            output="ok",
            fence=second.fence,
        )
        final = await ledger.get(workspace_id="ws-1", invocation_id="inv-1")
        assert final is not None and final.terminal and final.status == STATUS_SUCCEEDED

    asyncio.run(run())


def test_in_memory_renew_heartbeat_keeps_ownership() -> None:
    ledger = InMemorySandboxInvocationLedger()

    async def run() -> None:
        first = await ledger.claim(**_claim_kwargs("ws-hb", "inv-hb", owner_id="owner-a"))
        for _ in range(3):
            ok = await ledger.renew(
                workspace_id="ws-hb",
                invocation_id="inv-hb",
                fence=first.fence,
                lease_seconds=LEASE,
            )
            assert ok
            await asyncio.sleep(LEASE / 2)
        # Lease stayed alive through the heartbeats: no takeover.
        second = await ledger.claim(**_claim_kwargs("ws-hb", "inv-hb", owner_id="owner-b"))
        assert second.kind == "in_progress"

    asyncio.run(run())


def test_in_memory_renew_rejected_for_superseded_generation() -> None:
    ledger = InMemorySandboxInvocationLedger()

    async def run() -> None:
        first = await ledger.claim(**_claim_kwargs("ws-sup", "inv-sup", owner_id="owner-a"))
        await asyncio.sleep(LEASE + 0.05)
        second = await ledger.takeover(
            workspace_id="ws-sup", invocation_id="inv-sup", owner_id="owner-b"
        )
        assert second.kind == "takeover"
        ok = await ledger.renew(
            workspace_id="ws-sup",
            invocation_id="inv-sup",
            fence=first.fence,
        )
        assert ok is False

    asyncio.run(run())


def test_in_memory_list_expired_scans_only_nonterminal() -> None:
    ledger = InMemorySandboxInvocationLedger()

    async def run() -> None:
        await ledger.claim(**_claim_kwargs("ws-l1", "inv-l1", owner_id="owner-a"))
        await asyncio.sleep(LEASE + 0.05)
        live = await ledger.claim(
            **_claim_kwargs("ws-l2", "inv-l2", owner_id="owner-b", lease_seconds=30.0)
        )
        await ledger.complete(
            workspace_id="ws-l2",
            invocation_id="inv-l2",
            status=STATUS_UNKNOWN,
            fence=live.fence,
        )
        expired = await ledger.list_expired(limit=10)
        ids = {(row.workspace_id, row.invocation_id) for row in expired}
        assert ("ws-l1", "inv-l1") in ids
        assert ("ws-l2", "inv-l2") not in ids  # terminal rows never re-scanned

    asyncio.run(run())


def test_in_memory_complete_failure_injection_preserves_row() -> None:
    """S5-01 window E: the terminal write fails; the row stays non-terminal
    (never faked succeeded) for the reconciler to converge."""
    ledger = InMemorySandboxInvocationLedger()

    async def run() -> None:
        first = await ledger.claim(**_claim_kwargs("ws-f", "inv-f", owner_id="owner-a"))
        ledger.fail_complete_next = True
        with pytest.raises(SandboxLedgerError):
            await ledger.complete(
                workspace_id="ws-f",
                invocation_id="inv-f",
                status=STATUS_SUCCEEDED,
                fence=first.fence,
            )
        row = await ledger.get(workspace_id="ws-f", invocation_id="inv-f")
        assert row is not None and not row.terminal and row.status == "pending"
        # The owner still holds the row and can retry the terminal write.
        await ledger.complete(
            workspace_id="ws-f",
            invocation_id="inv-f",
            status=STATUS_SUCCEEDED,
            fence=first.fence,
        )
        row = await ledger.get(workspace_id="ws-f", invocation_id="inv-f")
        assert row is not None and row.terminal

    asyncio.run(run())


def test_fence_token_never_reused() -> None:
    fence_a = new_fence("owner-a")
    fence_b = new_fence("owner-a")
    assert fence_a.token != fence_b.token


def test_postgres_ledger_semantics() -> None:
    dsn = os.getenv("POSTGRES_DSN", "postgresql://map:map@127.0.0.1:15432/map")
    suffix = uuid4().hex[:8]
    ledger = PostgresSandboxInvocationLedger(dsn)

    async def run() -> None:
        ws = f"ws-{suffix}"
        inv = f"inv-{suffix}"
        try:
            first = await ledger.claim(**_claim_kwargs(ws, inv, lease_seconds=30.0))
        except SandboxLedgerError as exc:  # optional local PG / migration
            pytest.skip(f"postgres ledger unavailable: {exc}")
        assert first.kind == "owned"
        second = await ledger.claim(**_claim_kwargs(ws, inv, owner_id="owner-b"))
        assert second.kind == "in_progress"
        conflict = await ledger.claim(
            **_claim_kwargs(ws, inv, command="echo other")
        )
        assert conflict.kind == "conflict"
        await ledger.record_created(
            workspace_id=ws, invocation_id=inv, sandbox_id="sb-x", fence=first.fence
        )
        await ledger.complete(
            workspace_id=ws,
            invocation_id=inv,
            status=STATUS_SUCCEEDED,
            output="ok",
            fence=first.fence,
        )
        replay = await ledger.claim(**_claim_kwargs(ws, inv))
        assert replay.kind == "replay"
        assert replay.record is not None and replay.record.output == "ok"
        await ledger.close()

    asyncio.run(run())


def test_postgres_takeover_after_lease_expiry() -> None:
    dsn = os.getenv("POSTGRES_DSN", "postgresql://map:map@127.0.0.1:15432/map")
    suffix = uuid4().hex[:8]
    ledger = PostgresSandboxInvocationLedger(dsn)

    async def run() -> None:
        ws = f"ws-{suffix}"
        inv = f"inv-{suffix}"
        try:
            first = await ledger.claim(**_claim_kwargs(ws, inv, owner_id="owner-a"))
        except SandboxLedgerError as exc:
            pytest.skip(f"postgres ledger unavailable: {exc}")
        assert first.kind == "owned"
        await asyncio.sleep(LEASE + 0.2)
        second = await ledger.claim(**_claim_kwargs(ws, inv, owner_id="owner-b"))
        assert second.kind == "takeover", second
        assert second.fence is not None and second.fence.attempt == 1
        with pytest.raises(SandboxOwnershipLost):
            await ledger.complete(
                workspace_id=ws,
                invocation_id=inv,
                status=STATUS_SUCCEEDED,
                fence=first.fence,
            )
        await ledger.record_created(
            workspace_id=ws, invocation_id=inv, sandbox_id="sb-t", fence=second.fence
        )
        await ledger.complete(
            workspace_id=ws,
            invocation_id=inv,
            status=STATUS_SUCCEEDED,
            output="ok",
            fence=second.fence,
        )
        final = await ledger.get(workspace_id=ws, invocation_id=inv)
        assert final is not None and final.terminal
        assert final.status == STATUS_SUCCEEDED
        expired = await ledger.list_expired(limit=10)
        assert not any(r.workspace_id == ws for r in expired)
        await ledger.close()

    asyncio.run(run())
