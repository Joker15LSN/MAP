"""S5-01 crash-window victim process (spawned by test_sandbox_crash_recovery).

This module is NOT a test. The parent test spawns it with
sys.executable, waits for the barrier file of the requested window and
SIGKILLs it, reproducing an owner death at a deterministic point:

- after_claim    : the row exists (pending) but create was never sent;
- after_create   : the create RESPONSE arrived but record_created never ran;
- after_execute  : the execute RESPONSE arrived but complete never ran;
- after_complete : the terminal state was written but destroy never ran.
"""

from __future__ import annotations

import asyncio
import os
import sys

from map_core.service.opensandbox_client import (
    OpenSandboxClient,
    SandboxIdentity,
    SandboxResourceLimits,
)
from map_core.service.sandbox_ledger import (
    PostgresSandboxInvocationLedger,
    build_create_key,
    build_execute_key,
    normalize_request_digest,
)

_WINDOWS = {"after_claim", "after_create", "after_execute", "after_complete"}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


async def _main() -> int:
    window = _env("MAP_CRASH_WINDOW", "after_claim")
    if window not in _WINDOWS:
        print(f"unknown crash window {window!r}", file=sys.stderr)
        return 2
    barrier = _env("MAP_CRASH_BARRIER")
    if not barrier:
        print("MAP_CRASH_BARRIER is required", file=sys.stderr)
        return 2

    dsn = _env("POSTGRES_DSN")
    identity = SandboxIdentity(
        workspace_id=_env("MAP_CRASH_WORKSPACE", "ws-crash"),
        run_id=_env("MAP_CRASH_RUN", "run-crash"),
        step_id=_env("MAP_CRASH_STEP", "step-1"),
        attempt_id=_env("MAP_CRASH_ATTEMPT", "att-1"),
        invocation_id=_env("MAP_CRASH_INVOCATION", "inv-crash"),
        client_request_id=_env("MAP_CRASH_CLIENT_REQUEST", "req-crash"),
    )
    command = _env("MAP_CRASH_COMMAND", "echo crash-window")
    limits = SandboxResourceLimits()
    digest = normalize_request_digest(command=command, limits=limits.to_dict())
    create_key = build_create_key(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=digest,
    )
    execute_key = build_execute_key(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=digest,
    )
    lease = float(_env("MAP_CRASH_LEASE", "0.5"))

    ledger = PostgresSandboxInvocationLedger(dsn)
    claim = await ledger.claim(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=digest,
        create_key=create_key,
        execute_key=execute_key,
        request_payload={
            "command": command,
            "limits": limits.to_dict(),
            "identity": identity.to_dict(),
        },
        owner_id="crash-victim",
        lease_seconds=lease,
    )
    if claim.kind != "owned":
        print(f"victim claim failed: {claim.kind}", file=sys.stderr)
        return 2

    def _signal(marker: str) -> None:
        with open(barrier, "w", encoding="utf-8") as fh:
            fh.write(marker + "\n")

    if window == "after_claim":
        _signal("after_claim")
        await asyncio.sleep(3600)
        return 0

    client = OpenSandboxClient.from_env()
    created = await client.create_sandbox(
        identity, limits, idempotency_key=create_key
    )
    if window == "after_create":
        _signal("after_create")
        await asyncio.sleep(3600)
        return 0

    sandbox_id = str(created.get("sandbox_id") or "")
    if not sandbox_id:
        print("create response missing sandbox_id", file=sys.stderr)
        return 2
    await ledger.record_created(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        sandbox_id=sandbox_id,
        fence=claim.fence,
    )
    if window == "after_execute":
        # Execute lands, then the victim dies BEFORE recording the outcome.
        await client.execute(
            sandbox_id,
            identity,
            command,
            limits.timeout_seconds,
            idempotency_key=execute_key,
        )
        _signal("after_execute")
        await asyncio.sleep(3600)
        return 0

    outcome = await client.execute(
        sandbox_id,
        identity,
        command,
        limits.timeout_seconds,
        idempotency_key=execute_key,
    )
    await ledger.complete(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        status="succeeded",
        fence=claim.fence,
        sandbox_id=sandbox_id,
        output=str(outcome.get("output") or ""),
        server_state=outcome,
    )
    _signal("after_complete")
    await asyncio.sleep(3600)  # killed before the best-effort destroy
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
