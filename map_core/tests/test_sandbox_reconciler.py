"""S5-01: the durable SandboxReconciler converges crashed invocations.

Covers the review's crash windows against an in-memory ledger double:

- a pending row whose owner died before create: the reconciler takes the
  row over and drives create + execute (exactly once each);
- a created row whose owner died after execute (side effect landed,
  complete never written): the reconciler reuses the server-side execution
  by key WITHOUT re-issuing the command;
- a created row whose sandbox is no longer queryable and the server cannot
  prove non-execution: the row fails closed to unknown, never blind-replay.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest import mock

import httpx
import pytest

from map_core.service.opensandbox_client import (
    IDEMPOTENCY_HEADER,
    OpenSandboxClient,
    SandboxIdentity,
)
from map_core.service.sandbox_ledger import (
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    InMemorySandboxInvocationLedger,
    build_create_key,
    build_execute_key,
    normalize_request_digest,
)
from map_core.service.sandbox_tools import (
    SandboxReconciler,
    set_sandbox_ledger,
)

LIMITS = {
    "cpu_seconds": 30,
    "memory_mb": 512,
    "disk_mb": 1024,
    "max_output_bytes": 65536,
    "timeout_seconds": 30,
}

IDENTITY = SandboxIdentity(
    workspace_id="ws-r",
    run_id="run-1",
    step_id="step-1",
    attempt_id="att-1",
    invocation_id="inv-r",
    client_request_id="req-1",
)


class FakeOpenSandbox:
    """In-memory OpenSandbox 0.2.2 double with per-key idempotent dedup."""

    def __init__(self, *, sandbox_gone: bool = False) -> None:
        self.sandbox_gone = sandbox_gone
        self.create_calls: list[str] = []
        self.execute_calls: list[str] = []
        self.sandboxes: dict[str, dict] = {}
        self._creates: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method == "POST" and path == "/api/v1/sandboxes":
            payload = json.loads(request.content)
            if key and key in self._creates:
                sandbox_id = self._creates[key]
                self.create_calls.append(key)
                return httpx.Response(
                    201, json={"sandbox_id": sandbox_id, "status": "ready"}
                )
            sandbox_id = f"sb-{len(self.sandboxes) + 1}"
            if key:
                self._creates[key] = sandbox_id
            self.create_calls.append(key)
            self.sandboxes[payload["workspace_id"]] = {
                "sandbox_id": sandbox_id,
                "status": "ready",
                "executions": [],
            }
            return httpx.Response(
                201, json={"sandbox_id": sandbox_id, "status": "ready"}
            )
        if request.method == "POST" and path.endswith("/execute"):
            sandbox_id = path.rsplit("/", 2)[-2]
            payload = json.loads(request.content)
            sandbox = self.sandboxes.get(payload.get("workspace_id"))
            if sandbox is None or sandbox["sandbox_id"] != sandbox_id:
                return httpx.Response(404, json={"error": "unknown sandbox"})
            for execution in sandbox["executions"]:
                if execution.get("key") == key:
                    return httpx.Response(
                        200,
                        json={
                            "sandbox_id": sandbox_id,
                            "status": "completed",
                            "exit_code": 0,
                            "output": execution["output"],
                        },
                    )
            executed = {
                "key": key,
                "command": payload.get("command"),
                "output": f"ok: {payload.get('command')}",
            }
            self.execute_calls.append(key)
            sandbox["executions"].append(executed)
            return httpx.Response(
                200,
                json={
                    "sandbox_id": sandbox_id,
                    "status": "completed",
                    "exit_code": 0,
                    "output": executed["output"],
                },
            )
        if request.method == "GET" and "/api/v1/sandboxes/" in path:
            if self.sandbox_gone:
                return httpx.Response(404, json={"error": "unknown sandbox"})
            sandbox_id = path.rsplit("/", 1)[-1]
            for sandbox in self.sandboxes.values():
                if sandbox["sandbox_id"] == sandbox_id:
                    return httpx.Response(200, json=sandbox)
            return httpx.Response(404, json={"error": "unknown sandbox"})
        if request.method == "DELETE":
            sandbox_id = path.rsplit("/", 1)[-1]
            for ws, sandbox in list(self.sandboxes.items()):
                if sandbox["sandbox_id"] == sandbox_id:
                    del self.sandboxes[ws]
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "no route"})


def _client(server: FakeOpenSandbox) -> OpenSandboxClient:
    return OpenSandboxClient(
        base_url="https://sandbox.test",
        api_key="key-1234567890abcdef",
        transport=server.transport(),
    )


def _claim_args(workspace_id="ws-r", invocation_id="inv-r", command="echo hi"):
    digest = normalize_request_digest(command=command, limits=LIMITS)
    return {
        "workspace_id": workspace_id,
        "invocation_id": invocation_id,
        "request_digest": digest,
        "create_key": build_create_key(
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            request_digest=digest,
        ),
        "execute_key": build_execute_key(
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            request_digest=digest,
        ),
        "request_payload": {
            "command": command,
            "limits": LIMITS,
            "identity": IDENTITY.to_dict(),
        },
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ledger = InMemorySandboxInvocationLedger()
    set_sandbox_ledger(ledger)
    monkeypatch.setenv("MAP_OPENSANDBOX_URL", "https://sandbox.test")
    monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")
    monkeypatch.setenv("MAP_SANDBOX_LEASE_SECONDS", "0.1")
    yield ledger
    set_sandbox_ledger(None)


async def _expire(ledger, ws, inv) -> None:
    row = await ledger.get(workspace_id=ws, invocation_id=inv)
    assert row is not None
    ledger._rows[(ws, inv)] = replace(row, lease_expires_at=0.0)  # noqa: SLF001


def test_reconciler_drives_pending_row_after_owner_crash(_clean, monkeypatch) -> None:
    server = FakeOpenSandbox()
    ledger = _clean

    async def run() -> None:
        claim = await ledger.claim(
            **_claim_args(), owner_id="crashed-owner", lease_seconds=0.1
        )
        assert claim.kind == "owned"
        await asyncio.sleep(0.2)  # owner dies without any write
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            driven = await SandboxReconciler(ledger, interval_s=0.1, lease_seconds=5.0).reconcile_once()
        assert driven == 1
        assert len(server.create_calls) == 1
        assert len(server.execute_calls) == 1
        row = await ledger.get(workspace_id="ws-r", invocation_id="inv-r")
        assert row is not None and row.terminal and row.status == STATUS_SUCCEEDED
        assert row.output == "ok: echo hi"

    asyncio.run(run())


def test_reconciler_reuses_server_execution_without_reexecuting(_clean, monkeypatch) -> None:
    server = FakeOpenSandbox()
    ledger = _clean

    async def run() -> None:
        claim = await ledger.claim(
            **_claim_args(), owner_id="crashed-owner", lease_seconds=5.0
        )
        client = _client(server)
        created = await client.create_sandbox(IDENTITY, idempotency_key=claim.record.create_key)
        await ledger.record_created(
            workspace_id="ws-r",
            invocation_id="inv-r",
            sandbox_id=created["sandbox_id"],
            fence=claim.fence,
        )
        # The crashed owner already executed remotely (side effect landed),
        # but died before writing the terminal state.
        await client.execute(
            created["sandbox_id"],
            IDENTITY,
            "echo hi",
            idempotency_key=claim.record.execute_key,
        )
        await client.aclose()
        await _expire(ledger, "ws-r", "inv-r")
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            driven = await SandboxReconciler(ledger, lease_seconds=5.0).reconcile_once()
        assert driven == 1
        assert len(server.execute_calls) == 1, "must reuse the prior execution"
        row = await ledger.get(workspace_id="ws-r", invocation_id="inv-r")
        assert row is not None and row.terminal and row.status == STATUS_SUCCEEDED
        assert row.output == "ok: echo hi"

    asyncio.run(run())


def test_reconciler_fails_closed_when_resumed_sandbox_unqueryable(_clean, monkeypatch) -> None:
    server = FakeOpenSandbox(sandbox_gone=True)
    ledger = _clean

    async def run() -> None:
        claim = await ledger.claim(
            **_claim_args(), owner_id="crashed-owner", lease_seconds=5.0
        )
        client = _client(server)
        created = await client.create_sandbox(IDENTITY, idempotency_key=claim.record.create_key)
        await ledger.record_created(
            workspace_id="ws-r",
            invocation_id="inv-r",
            sandbox_id=created["sandbox_id"],
            fence=claim.fence,
        )
        await client.aclose()
        await _expire(ledger, "ws-r", "inv-r")
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            driven = await SandboxReconciler(ledger, lease_seconds=5.0).reconcile_once()
        assert driven == 1
        assert len(server.execute_calls) == 0, "must never re-issue execute"
        row = await ledger.get(workspace_id="ws-r", invocation_id="inv-r")
        assert row is not None and row.terminal
        assert row.status == STATUS_UNKNOWN  # cannot prove non-execution

    asyncio.run(run())
