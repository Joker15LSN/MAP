"""S4-01: sandbox_exec_tool durable idempotency + isolation boundary.

These tests pin the review-mandated behavior: a PostgreSQL-style
SandboxInvocationLedger (injected here as the in-memory double with the
SAME claim semantics) is the source of truth; the remote server is queried
only while the sandbox still exists; a destroyed sandbox is never assumed
queryable; and a lost create/execute response never blindly resends a
mutation. The fake server's DELETE really removes sandbox state so the
reconciliation path must rely on the ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest import mock

import httpx
import pytest

from map_core.service.agent.base import AgentRequest, ToolResult
from map_core.service.opensandbox_client import (
    CONNECT_ERROR,
    IDEMPOTENCY_HEADER,
    UNKNOWN_OUTCOME,
    OpenSandboxClient,
    OpenSandboxClientError,
    SandboxIdentity,
    SandboxResourceLimits,
)
from map_core.service.sandbox_ledger import (
    IDEMPOTENCY_CONFLICT,
    LEDGER_ERROR,
    STATUS_CREATED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    InMemorySandboxInvocationLedger,
    SandboxLedgerError,
    normalize_request_digest,
)
from map_core.service.sandbox_tools import (
    CAPABILITY_DISABLED,
    IDENTITY_INCOMPLETE,
    PROTOCOL_VERSION,
    _sandbox_execute_handler,
    build_sandbox_tools,
    set_sandbox_ledger,
)

AUTH_HEADER = "OPEN-SANDBOX-API-KEY"


FULL_IDENTITY = {
    "workspace_id": "ws-1",
    "run_id": "run-1",
    "step_id": "step-1",
    "attempt_id": "att-1",
    "invocation_id": "inv-1",
    "client_request_id": "req-1",
}


def _request(**extra) -> AgentRequest:
    payload = dict(FULL_IDENTITY)
    payload.update(extra)
    return AgentRequest(query="run it", staff_code="pytest", extra=payload)


_ACTIVE_LEDGER: InMemorySandboxInvocationLedger | None = None


@pytest.fixture(autouse=True)
def _ledger_and_env(monkeypatch):
    global _ACTIVE_LEDGER
    _ACTIVE_LEDGER = InMemorySandboxInvocationLedger()
    set_sandbox_ledger(_ACTIVE_LEDGER)
    for key in ("MAP_OPENSANDBOX_URL", "MAP_OPENSANDBOX_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield
    set_sandbox_ledger(None)
    _ACTIVE_LEDGER = None


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("MAP_OPENSANDBOX_URL", "https://sandbox.test")
    monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")


class FakeSandboxServer:
    """In-memory OpenSandbox 0.2.2 contract double.

    DELETE really deletes the sandbox (post-destroy the server state is not
    queryable), matching the real server contract the review demands.
    """

    def __init__(
        self,
        *,
        fail_create_with: type[Exception] | None = None,
        fail_execute_with: type[Exception] | None = None,
        execute_then_timeout: bool = False,
    ) -> None:
        self.fail_create_with = fail_create_with
        self.fail_execute_with = fail_execute_with
        self.execute_then_timeout = execute_then_timeout
        self.create_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self.destroy_calls: list[str] = []
        self.sandboxes: dict[str, dict] = {}
        self._idempotent_creates: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method == "POST" and path == "/api/v1/sandboxes":
            if self.fail_create_with is not None:
                raise self.fail_create_with("create failed")
            payload = json_loads(request)
            if key and key in self._idempotent_creates:
                sandbox_id = self._idempotent_creates[key]
                self.create_calls.append(
                    {"key": key, "workspace_id": payload.get("workspace_id"),
                     "limits": payload.get("limits"), "sandbox_id": sandbox_id}
                )
                existing = self.sandboxes.get(payload.get("workspace_id"))
                status = existing["status"] if existing else "ready"
                return httpx.Response(
                    201, json={"sandbox_id": sandbox_id, "status": status}
                )
            sandbox_id = f"sb-{len(self.sandboxes) + 1}"
            if key:
                self._idempotent_creates[key] = sandbox_id
            self.create_calls.append(
                {"key": key, "workspace_id": payload.get("workspace_id"),
                 "limits": payload.get("limits"), "sandbox_id": sandbox_id}
            )
            self.sandboxes[payload.get("workspace_id")] = {
                "sandbox_id": sandbox_id,
                "status": "ready",
                "executions": [],
            }
            return httpx.Response(201, json={"sandbox_id": sandbox_id, "status": "ready"})
        if request.method == "POST" and path.endswith("/execute"):
            sandbox_id = path.rsplit("/", 2)[-2]
            payload = json_loads(request)
            workspace_id = payload.get("workspace_id")
            sandbox = self.sandboxes.get(workspace_id)
            if sandbox is None or sandbox["sandbox_id"] != sandbox_id:
                return httpx.Response(404, json={"error": "unknown sandbox"})
            if self.fail_execute_with is not None:
                raise self.fail_execute_with("execute failed")
            executed = {
                "key": key,
                "command": payload.get("command"),
                "output": f"ok: {payload.get('command')}",
            }
            self.execute_calls.append(executed)
            sandbox["executions"].append(executed)
            if self.execute_then_timeout:
                # Side effect landed, then the response was lost.
                raise httpx.TimeoutException("simulated lost execute response")
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
            sandbox_id = path.rsplit("/", 1)[-1]
            for sandbox in self.sandboxes.values():
                if sandbox["sandbox_id"] == sandbox_id:
                    return httpx.Response(200, json=sandbox)
            return httpx.Response(404, json={"error": "unknown sandbox"})
        if request.method == "DELETE":
            sandbox_id = path.rsplit("/", 1)[-1]
            self.destroy_calls.append(sandbox_id)
            for ws, sandbox in list(self.sandboxes.items()):
                if sandbox["sandbox_id"] == sandbox_id:
                    del self.sandboxes[ws]
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "no route"})


def json_loads(request: httpx.Request) -> dict:
    return __import__("json").loads(request.content)


def _client(server: FakeSandboxServer) -> OpenSandboxClient:
    return OpenSandboxClient(
        base_url="https://sandbox.test",
        api_key="key-" + "1234567890abcdef",
        transport=server.transport(),
    )


def _expected_digest(command: str) -> str:
    return normalize_request_digest(
        command=command, limits=SandboxResourceLimits().to_dict()
    )


class TestRegistryWiring:
    def test_registry_contains_the_production_tool(self) -> None:
        tools = build_sandbox_tools()
        assert "sandbox_exec_tool" in tools
        assert tools["sandbox_exec_tool"].handler is not None

    def test_dispatcher_registers_the_tool(self) -> None:
        from map_core.service.agent_dispatcher import (
            AgentDispatchConfig,
            AgentDispatcher,
        )

        class _FakeEngine:
            pass

        dispatcher = AgentDispatcher(llm=_FakeEngine(), tool_registry={})  # type: ignore[arg-type]
        dispatcher._register_dynamic_tools(AgentDispatchConfig())
        assert "sandbox_exec_tool" in dispatcher.tool_registry


class TestUnconfiguredCapability:
    def test_unconfigured_returns_capability_disabled(self) -> None:
        result = asyncio.run(
            _sandbox_execute_handler({"command": "echo hi"}, _request(), "parid")
        )
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert CAPABILITY_DISABLED in result.error
        assert result.data_source is not None
        # S7-03: the machine-readable code the worker maps on is
        # CAPABILITY_DISABLED (terminal), not the client-internal
        # OPENSANDBOX_CONFIG_MISSING detail.
        assert result.data_source.get("error_code") == CAPABILITY_DISABLED


class TestFullChain:
    def test_full_chain_with_durable_identity(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            result = asyncio.run(
                _sandbox_execute_handler(
                    {"command": "echo hi"},
                    _request(workspace_id="ws-1", run_id="run-1", step_id="step-1"),
                    "parid",
                )
            )
        assert result.success is True
        assert "ok: echo hi" in result.content
        assert len(server.create_calls) == 1
        assert server.create_calls[0]["workspace_id"] == "ws-1"
        assert server.create_calls[0]["limits"]["timeout_seconds"] == 30
        assert len(server.execute_calls) == 1
        assert len(server.destroy_calls) == 1
        # DELETE really deleted: the server no longer knows the sandbox.
        assert server.sandboxes == {}
        # idempotency keys are scoped to workspace + normalized request digest.
        create_key = server.create_calls[0]["key"]
        execute_key = server.execute_calls[0]["key"]
        digest = _expected_digest("echo hi")
        assert create_key == f"create:ws-1:inv-1:{digest}"
        assert execute_key == f"execute:ws-1:inv-1:{digest}"
        meta = result.data_source
        assert meta["source"] == "opensandbox"
        assert meta["protocol_version"] == PROTOCOL_VERSION
        assert meta["identity"]["run_id"] == "run-1"
        assert meta["server_state"]["status"] == "completed"


class TestFailureMatrix:
    def test_execute_timeout_reconciles_without_replay(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer(fail_execute_with=httpx.TimeoutException)
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            result = asyncio.run(
                _sandbox_execute_handler(
                    {"command": "rm -rf /data"}, _request(workspace_id="ws-9"), "parid"
                )
            )
        assert result.success is False
        assert UNKNOWN_OUTCOME in result.error
        assert len(server.execute_calls) == 0
        assert result.data_source["server_state"]["status"] == "ready"

    def test_lost_execute_response_recovers_when_server_executed(self, monkeypatch) -> None:
        """Execute landed then the response was lost: reconciliation takes the
        server result without re-issuing the command."""
        _configure(monkeypatch)
        server = FakeSandboxServer(execute_then_timeout=True)
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            result = asyncio.run(
                _sandbox_execute_handler(
                    {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                )
            )
        assert result.success is True
        assert "ok: echo hi" in result.content
        assert len(server.execute_calls) == 1, "must not re-issue the command"
        assert len(server.destroy_calls) == 1

    def test_lost_create_response_fails_closed(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer(fail_create_with=httpx.TimeoutException)
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            result = asyncio.run(
                _sandbox_execute_handler(
                    {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                )
            )
        assert result.success is False
        assert UNKNOWN_OUTCOME in result.error
        assert len(server.execute_calls) == 0
        assert _ACTIVE_LEDGER is not None
        record = asyncio.run(
            _ACTIVE_LEDGER.get(workspace_id="ws-1", invocation_id="inv-1")
        )
        assert record is not None and record.status == STATUS_UNKNOWN

    def test_network_failure_reports_unreachable(self, monkeypatch) -> None:
        _configure(monkeypatch)

        class _DownTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("connection refused")

        client = OpenSandboxClient(
            base_url="https://sandbox.test",
            api_key="key-" + "1234567890abcdef",
            transport=_DownTransport(),
        )
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: client):
            with pytest.raises(OpenSandboxClientError) as exc_info:
                asyncio.run(
                    _sandbox_execute_handler(
                        {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                    )
                )
        assert exc_info.value.code == CONNECT_ERROR

    def test_idempotency_key_replay_creates_no_duplicate_side_effects(
        self, monkeypatch
    ) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        client = _client(server)
        identity = SandboxIdentity(
            workspace_id="ws-1",
            run_id="run-1",
            step_id="step-1",
            attempt_id="att-1",
            invocation_id="inv-1",
            client_request_id="req-same-key",
        )
        first = asyncio.run(client.create_sandbox(identity))
        second = asyncio.run(client.create_sandbox(identity))
        assert first["sandbox_id"] == second["sandbox_id"]
        assert len(server.create_calls) == 2
        assert len({c["sandbox_id"] for c in server.create_calls}) == 1

    def test_cross_workspace_sandboxes_are_isolated(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        client = _client(server)
        ws_a = SandboxIdentity(
            workspace_id="ws-a", run_id="r", step_id="s", attempt_id="a",
            invocation_id="i", client_request_id="ka",
        )
        ws_b = SandboxIdentity(
            workspace_id="ws-b", run_id="r", step_id="s", attempt_id="a",
            invocation_id="i", client_request_id="kb",
        )
        sandbox_a = asyncio.run(client.create_sandbox(ws_a))
        sandbox_b = asyncio.run(client.create_sandbox(ws_b))
        assert sandbox_a["sandbox_id"] != sandbox_b["sandbox_id"]
        payload = {
            "sandbox_id": sandbox_a["sandbox_id"],
            "command": "cat /etc/passwd",
            "timeout_seconds": 5,
            **ws_b.to_dict(),
        }
        response = asyncio.run(
            client._client.post(
                f"/api/v1/sandboxes/{sandbox_a['sandbox_id']}/execute",
                json=payload,
                headers=client._headers(ws_b),
            )
        )
        assert response.status_code == 404

    def test_secret_never_leaks_in_results(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            result = asyncio.run(
                _sandbox_execute_handler(
                    {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                )
            )
        serialized = result.model_dump_json()
        assert "key-1234567890abcdef" not in serialized


class TestS4IdentityContract:
    def test_missing_identity_field_fails_closed_without_network(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            for missing_field in (
                "workspace_id", "run_id", "step_id", "attempt_id",
                "invocation_id", "client_request_id",
            ):
                bad_extra = dict(FULL_IDENTITY)
                bad_extra[missing_field] = None
                result = asyncio.run(
                    _sandbox_execute_handler(
                        {"command": "echo hi"}, _request(**bad_extra), "parid"
                    )
                )
                assert result.success is False
                assert IDENTITY_INCOMPLETE in result.error
                assert missing_field in result.error
        assert server.create_calls == []
        assert server.execute_calls == []

    def test_sandbox_tool_is_in_the_single_tool_schema(self) -> None:
        from map_core.service.agent.tool_registry import find_invalid_tool_names

        assert find_invalid_tool_names(["sandbox_exec_tool"]) == []
        assert "sandbox_exec_tool" not in find_invalid_tool_names(
            ["sandbox_exec_tool", "no_such_tool"]
        )


class TestS4LedgerExactlyOnce:
    def _run(self, server, command="echo hi", **extra):
        return asyncio.run(
            _sandbox_execute_handler({"command": command}, _request(**extra), "parid")
        )

    def test_same_invocation_executes_exactly_once(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            first = self._run(server, invocation_id="inv-once")
            second = self._run(server, invocation_id="inv-once")
        assert first.success is True
        assert second.success is True
        assert len(server.execute_calls) == 1, server.execute_calls

    def test_distinct_create_and_execute_idempotency_keys(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            self._run(server, invocation_id="inv-keys")
        create_key = server.create_calls[0]["key"]
        execute_key = server.execute_calls[0]["key"]
        assert create_key.startswith("create:")
        assert execute_key.startswith("execute:")
        assert create_key != execute_key

    def test_restart_recovers_from_ledger_not_destroyed_server(self, monkeypatch) -> None:
        """After a successful run the server's sandbox is destroyed; a retry
        (e.g. a restarted worker) must replay from the LEDGER, never from the
        now-unqueryable server."""
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            first = self._run(server, invocation_id="inv-restart")
            assert first.success is True
            assert len(server.execute_calls) == 1
            assert server.sandboxes == {}, "sandbox must be really destroyed"
            second = self._run(server, invocation_id="inv-restart")
        assert second.success is True
        assert second.content == first.content
        assert len(server.execute_calls) == 1, "must not re-issue the command"
        assert len(server.create_calls) == 1, "must not re-create the sandbox"

    def test_concurrent_calls_execute_exactly_once(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()

        async def run_many() -> list[ToolResult]:
            return list(
                await asyncio.gather(
                    *(
                        _sandbox_execute_handler(
                            {"command": "echo hi"},
                            _request(workspace_id="ws-1", invocation_id="inv-conc"),
                            "parid",
                        )
                        for _ in range(50)
                    )
                )
            )

        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            results = asyncio.run(run_many())
        assert all(r.success for r in results)
        assert len(server.execute_calls) == 1, server.execute_calls
        assert len({r.content for r in results}) == 1

    def test_cross_workspace_same_invocation_isolated(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            a = self._run(server, workspace_id="ws-a", invocation_id="inv-x")
            b = self._run(server, workspace_id="ws-b", invocation_id="inv-x")
        assert a.success is True
        assert b.success is True
        assert len(server.execute_calls) == 2
        create_keys = {c["key"] for c in server.create_calls}
        assert len(create_keys) == 2, "distinct workspaces must not reuse keys"
        assert a.content == b.content  # same command, but isolated ledgers

    def test_same_invocation_different_payload_conflicts(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
            first = self._run(server, command="echo one", workspace_id="ws-1")
            second = self._run(server, command="echo two", workspace_id="ws-1")
        assert first.success is True
        assert second.success is False
        assert IDEMPOTENCY_CONFLICT in second.error
        assert len(server.execute_calls) == 1, "old result must never be replayed"
"""S5-01: crash-window convergence for the sandbox execution tool.

These tests reproduce the review's counter-example - an owner dies between
the remote create/execute and the ledger write, or the terminal write
itself fails - and pin the fixed behavior:

- a retry NEVER hangs in pending forever: once the owner's lease expires the
  retry takes the row over atomically and FINISHES the remote flow;
- a failed terminal write NEVER destroys the only recoverable sandbox and
  NEVER reports success; the row stays non-terminal for the reconciler;
- remote create/execute counts stay <= 1 in every window (idempotency keys).
"""

FULL_IDENTITY = {
    "workspace_id": "ws-1",
    "run_id": "run-1",
    "step_id": "step-1",
    "attempt_id": "att-1",
    "invocation_id": "inv-1",
    "client_request_id": "req-1",
}


def _request(**extra) -> AgentRequest:
    payload = dict(FULL_IDENTITY)
    payload.update(extra)
    return AgentRequest(query="run it", staff_code="pytest", extra=payload)


class BlockingExecuteTransport(httpx.AsyncBaseTransport):
    """Holds the execute response until released (crash-before-complete)."""

    def __init__(self, server) -> None:
        self.server = server
        self.execute_seen = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/execute"):
            self.execute_seen.set()
            await self.release.wait()
        return self.server._handle(request)  # noqa: SLF001 - test double


def _configure_fast_lease(monkeypatch) -> None:
    monkeypatch.setenv("MAP_OPENSANDBOX_URL", "https://sandbox.test")
    monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")
    monkeypatch.setenv("MAP_SANDBOX_LEASE_SECONDS", "0.2")
    monkeypatch.setenv("MAP_SANDBOX_IN_PROGRESS_WAIT_SECONDS", "10")


def test_owner_crash_after_execute_converges_via_takeover(monkeypatch) -> None:
    """S5-01 counter-example fixed: owner dies with the execute response
    pending; the retry takes over after lease expiry and converges WITHOUT a
    second remote execution (the server dedupes by the execute key)."""
    _configure_fast_lease(monkeypatch)
    server = FakeSandboxServer()
    transport = BlockingExecuteTransport(server)

    def client_factory() -> OpenSandboxClient:
        return OpenSandboxClient(
            base_url="https://sandbox.test",
            api_key="key-1234567890abcdef",
            transport=transport,
        )

    async def run() -> None:
        with mock.patch.object(OpenSandboxClient, "from_env", client_factory):
            first_task = asyncio.create_task(
                _sandbox_execute_handler(
                    {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                )
            )
            await transport.execute_seen.wait()
            # Simulate the owner process dying before complete(): cancel it.
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first_task
            transport.release.set()
            # The retry (same invocation) must converge, not hang.
            result = await _sandbox_execute_handler(
                {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
            )
        assert result.success is True
        assert "ok: echo hi" in result.content
        assert len(server.execute_calls) == 1, "must never double-execute"
        assert len(server.create_calls) == 1, "must never double-create"
        assert len(server.destroy_calls) == 1
        record = await _ACTIVE_LEDGER.get(workspace_id="ws-1", invocation_id="inv-1")
        assert record is not None and record.terminal
        assert record.status == STATUS_SUCCEEDED

    asyncio.run(run())


def test_terminal_write_failure_never_destroys_and_never_fakes_success(
    monkeypatch,
) -> None:
    """S5-01 window E: ledger.complete() fails after the remote execute
    succeeded. The call must NOT destroy the sandbox and must NOT return
    success; the next attempt converges from the ledger + server state."""
    _configure_fast_lease(monkeypatch)
    server = FakeSandboxServer()

    def client_factory() -> OpenSandboxClient:
        return OpenSandboxClient(
            base_url="https://sandbox.test",
            api_key="key-1234567890abcdef",
            transport=server.transport(),
        )

    async def run() -> None:
        _ACTIVE_LEDGER.fail_complete_next = True
        with mock.patch.object(OpenSandboxClient, "from_env", client_factory):
            with pytest.raises(SandboxLedgerError) as exc_info:
                await _sandbox_execute_handler(
                    {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                )
        assert LEDGER_ERROR in str(exc_info.value)
        # The sandbox was NOT destroyed: it is the only recoverable fact.
        assert len(server.destroy_calls) == 0
        assert len(server.execute_calls) == 1
        record = await _ACTIVE_LEDGER.get(workspace_id="ws-1", invocation_id="inv-1")
        assert record is not None and not record.terminal
        assert record.status == STATUS_CREATED
        assert record.sandbox_id is not None
        # Wait out the dead owner's lease, then converge.
        await asyncio.sleep(0.35)
        with mock.patch.object(OpenSandboxClient, "from_env", client_factory):
            result = await _sandbox_execute_handler(
                {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
            )
        assert result.success is True
        assert "ok: echo hi" in result.content
        assert len(server.execute_calls) == 1, "prior execution must be reused"
        assert len(server.destroy_calls) == 1
        record = await _ACTIVE_LEDGER.get(workspace_id="ws-1", invocation_id="inv-1")
        assert record is not None and record.terminal
        assert record.status == STATUS_SUCCEEDED

    asyncio.run(run())
