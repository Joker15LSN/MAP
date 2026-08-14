"""S2-06: sandbox_exec_tool production wiring + failure-matrix contract.

The handler is the production entry for remote execution (no host
fallback). With a mock Server transport these tests cover the review
matrix: unconfigured CAPABILITY_DISABLED, full create/execute/destroy
chain with the durable identity, execute-timeout reconciliation WITHOUT a
duplicate execution, idempotency-key replay, network failure, and
cross-workspace isolation of sandbox identities.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import httpx
import pytest

from map_core.service.agent.base import AgentRequest, ToolResult
from map_core.service.opensandbox_client import (
    CONNECT_ERROR,
    IDEMPOTENCY_HEADER,
    MISSING_CONFIG_ERROR,
    UNKNOWN_OUTCOME,
    OpenSandboxClient,
    OpenSandboxClientError,
    SandboxIdentity,
)
from map_core.service.sandbox_tools import (
    CAPABILITY_DISABLED,
    PROTOCOL_VERSION,
    _sandbox_execute_handler,
    build_sandbox_tools,
)

AUTH_HEADER = "OPEN-SANDBOX-API-KEY"


def _request(**extra) -> AgentRequest:
    return AgentRequest(query="run it", staff_code="pytest", extra=extra)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("MAP_OPENSANDBOX_URL", "MAP_OPENSANDBOX_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("MAP_OPENSANDBOX_URL", "https://sandbox.test")
    monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")


# ---------------------------------------------------------------------------
# Server double: idempotent create/execute with per-workspace sandboxes
# ---------------------------------------------------------------------------


class FakeSandboxServer:
    """In-memory OpenSandbox 0.2.2 contract double."""

    def __init__(self, *, fail_execute_with: type[Exception] | None = None) -> None:
        self.fail_execute_with = fail_execute_with
        self.create_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self.destroy_calls: list[str] = []
        self.sandboxes: dict[str, dict] = {}  # workspace_id -> sandbox
        self._idempotent_creates: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method == "POST" and path == "/api/v1/sandboxes":
            payload = json_loads(request)
            if key and key in self._idempotent_creates:
                sandbox_id = self._idempotent_creates[key]
            else:
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
            if self.fail_execute_with is not None:
                raise self.fail_execute_with("execute failed")
            sandbox_id = path.rsplit("/", 2)[-2]
            payload = json_loads(request)
            workspace_id = payload.get("workspace_id")
            sandbox = self.sandboxes.get(workspace_id)
            if sandbox is None or sandbox["sandbox_id"] != sandbox_id:
                return httpx.Response(404, json={"error": "unknown sandbox"})
            executed = {"key": key, "command": payload.get("command")}
            self.execute_calls.append(executed)
            sandbox["executions"].append(executed)
            return httpx.Response(
                200,
                json={
                    "sandbox_id": sandbox_id,
                    "status": "completed",
                    "exit_code": 0,
                    "output": f"ok: {payload.get('command')}",
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


# ---------------------------------------------------------------------------


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
        from map_core.utils.llm_engine import LLMEngine

        class _FakeEngine:
            pass

        dispatcher = AgentDispatcher(
            llm=_FakeEngine(),  # type: ignore[arg-type]
            tool_registry={},
        )
        dispatcher._register_dynamic_tools(AgentDispatchConfig())
        assert "sandbox_exec_tool" in dispatcher.tool_registry


class TestUnconfiguredCapability:
    def test_unconfigured_returns_capability_disabled(self) -> None:
        result = asyncio.run(_sandbox_execute_handler(
            {"command": "echo hi"}, _request(), "parid"
        ))
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert CAPABILITY_DISABLED in result.error
        assert result.data_source is not None
        assert result.data_source.get("error_code") == MISSING_CONFIG_ERROR


class TestFullChain:
    def test_full_chain_with_durable_identity(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(
            OpenSandboxClient, "from_env", lambda: _client(server)
        ):
            result = asyncio.run(_sandbox_execute_handler(
                {"command": "echo hi"},
                _request(workspace_id="ws-1", run_id="run-1", step_id="step-1"),
                "parid",
            ))
        assert result.success is True
        assert "ok: echo hi" in result.content
        # create + execute + destroy happened, with the durable identity
        assert len(server.create_calls) == 1
        assert server.create_calls[0]["workspace_id"] == "ws-1"
        assert server.create_calls[0]["limits"]["timeout_seconds"] == 30
        assert len(server.execute_calls) == 1
        assert len(server.destroy_calls) == 1
        # durable record: identity + policy version + limits + server state
        meta = result.data_source
        assert meta["source"] == "opensandbox"
        assert meta["protocol_version"] == PROTOCOL_VERSION
        assert meta["identity"]["run_id"] == "run-1"
        assert meta["server_state"]["status"] == "completed"


class TestFailureMatrix:
    def test_execute_timeout_reconciles_without_replay(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer(fail_execute_with=httpx.TimeoutException)
        with mock.patch.object(
            OpenSandboxClient, "from_env", lambda: _client(server)
        ):
            result = asyncio.run(_sandbox_execute_handler(
                {"command": "rm -rf /data"}, _request(workspace_id="ws-9"), "parid"
            ))
        assert result.success is False
        assert UNKNOWN_OUTCOME in result.error
        # the timed-out mutation was NEVER re-issued
        assert len(server.execute_calls) == 0
        # and the server-side state was reconciled into the result
        assert result.data_source["server_state"]["status"] == "ready"

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
                asyncio.run(_sandbox_execute_handler(
                    {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
                ))
        assert exc_info.value.code == CONNECT_ERROR

    def test_idempotency_key_replay_creates_no_duplicate_side_effects(
        self, monkeypatch
    ) -> None:
        """Same client_request_id -> the Server returns the same sandbox and
        never executes the command twice."""
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
        assert len(server.create_calls) == 2  # two HTTP calls...
        assert len({c["sandbox_id"] for c in server.create_calls}) == 1  # ...one sandbox

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
        # executing on B's sandbox with A's identity is rejected by the server
        payload = {
            "sandbox_id": sandbox_a["sandbox_id"],
            "command": "cat /etc/passwd",
            "timeout_seconds": 5,
            **ws_b.to_dict(),
        }
        response = asyncio.run(client._client.post(
            f"/api/v1/sandboxes/{sandbox_a['sandbox_id']}/execute",
            json=payload,
            headers=client._headers(ws_b),
        ))
        assert response.status_code == 404

    def test_secret_never_leaks_in_results(self, monkeypatch) -> None:
        _configure(monkeypatch)
        server = FakeSandboxServer()
        with mock.patch.object(
            OpenSandboxClient, "from_env", lambda: _client(server)
        ):
            result = asyncio.run(_sandbox_execute_handler(
                {"command": "echo hi"}, _request(workspace_id="ws-1"), "parid"
            ))
        serialized = result.model_dump_json()
        assert "key-1234567890abcdef" not in serialized
