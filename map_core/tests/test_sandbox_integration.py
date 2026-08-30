"""S4-01 integration: the real request path reaches the sandbox tool.

Verifies the identity wiring from the request boundary (run/attempt/
client_request) through ToolExecutor (step/invocation) into the sandbox
tool handler, end to end with a mock OpenSandbox server and the injected
in-memory ledger.
"""

from __future__ import annotations

from unittest import mock

import httpx
import pytest
from fastapi import Request
from starlette.datastructures import Headers

from map_core.routers import master_pipeline_router
from map_core.service.agent.base import AgentRequest
from map_core.service.agent.tool_executor import ToolExecutor
from map_core.service.agent.tool_runtime import ToolSet
from map_core.service.agentscope2.agent import AgentScopeSceneAgent
from map_core.service.master_pipeline import MasterPipeline
from map_core.service.opensandbox_client import IDEMPOTENCY_HEADER, OpenSandboxClient
from map_core.service.run_identity import resolve_run_identity
from map_core.service.sandbox_ledger import InMemorySandboxInvocationLedger
from map_core.service.sandbox_tools import build_sandbox_tools, set_sandbox_ledger
from tests.run_context_utils import run_with_run_context


def _make_http_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": Headers(headers).raw,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 8000),
        "state": {},
        "app": None,
    }
    return Request(scope)


class _FakeLLMConfig:
    model = "fake-model"
    base_url = "http://localhost:8000/v1"
    api_key = "fake-key"


class FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeLLMConfig()


class FakeSandboxServer:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.execute_calls: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if request.method == "POST" and path == "/api/v1/sandboxes":
            payload = json.loads(request.content)
            self.create_calls.append({"key": key, "payload": payload})
            return httpx.Response(201, json={"sandbox_id": "sb-1", "status": "ready"})
        if request.method == "POST" and path.endswith("/execute"):
            payload = json.loads(request.content)
            self.execute_calls.append({"key": key, "payload": payload})
            return httpx.Response(
                200,
                json={
                    "sandbox_id": "sb-1",
                    "status": "completed",
                    "output": "ok: " + str(payload.get("command")),
                },
            )
        if request.method == "GET" and "/api/v1/sandboxes/" in path:
            return httpx.Response(
                200, json={"sandbox_id": "sb-1", "status": "ready", "executions": []}
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "no route"})


def _client(server: FakeSandboxServer) -> OpenSandboxClient:
    return OpenSandboxClient(
        base_url="https://sandbox.test",
        api_key="key-1234567890abcdef",
        transport=server.transport(),
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("MAP_OPENSANDBOX_URL", "https://sandbox.test")
    monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")
    set_sandbox_ledger(InMemorySandboxInvocationLedger())
    yield
    set_sandbox_ledger(None)


def test_resolve_run_identity_honors_headers_and_falls_back() -> None:
    req = _make_http_request(
        {
            "X-Run-ID": "run-9",
            "X-Attempt-ID": "att-2",
            "X-Client-Request-ID": "creq-3",
        }
    )
    identity = resolve_run_identity(req, request_id="req-1", workspace_id="ws-1")
    assert identity["run_id"] == "run-9"
    assert identity["attempt_id"] == "att-2"
    assert identity["client_request_id"] == "creq-3"

    req2 = _make_http_request({})
    identity2 = resolve_run_identity(req2, request_id="req-2", workspace_id="ws-2")
    assert identity2["run_id"] == "req-2"
    assert identity2["attempt_id"] == "att-1"
    assert identity2["client_request_id"] == "req-2"


def test_master_pipeline_freezes_run_identity() -> None:
    req = _make_http_request(
        {
            "X-Request-ID": "req-1",
            "X-Workspace-ID": "ws-1",
            "X-Run-ID": "run-9",
            "X-Attempt-ID": "att-2",
            "X-Client-Request-ID": "creq-3",
        }
    )
    master_pipeline_router._apply_runtime_headers(req, request_token=None)
    master = MasterPipeline(request=None, http_request=req, tool_registry={})
    assert master.run_id == "run-9"
    assert master.attempt_id == "att-2"
    assert master.client_request_id == "creq-3"


def test_tool_executor_invokes_sandbox_tool_with_full_identity() -> None:
    """The real entry point: ToolExecutor -> sandbox tool handler, with the
    request-level fields from extra and step/invocation injected per call."""
    server = FakeSandboxServer()
    owner = AgentScopeSceneAgent(
        llm=FakeLLM(),
        name="TestAgent",
        system_prompt="test",
        additional_user_prompt="",
        tools=[],
        max_steps=3,
        force_tool_call=False,
        scene_post_summary=None,
    )
    tools = build_sandbox_tools()
    executor = ToolExecutor(
        owner=owner,
        toolset=ToolSet(list(tools.values())),
        tools_timeout=5.0,
        log_tag_getter=lambda: "[TestAgent AGENT]",
    )
    request = AgentRequest(
        query="run it",
        staff_code="pytest",
        extra={
            "workspace_id": "ws-1",
            "run_id": "run-1",
            "attempt_id": "att-1",
            "client_request_id": "req-1",
        },
    )
    with mock.patch.object(OpenSandboxClient, "from_env", lambda: _client(server)):
        result = run_with_run_context(
            lambda: executor.execute_tool(
                tool_name="sandbox_exec_tool",
                parid="p-1",
                args={"command": "echo hi"},
                request=request,
                step_index=3,
                tool_call_id="call-xyz",
            )
        )
    assert result.success is True
    assert "ok: echo hi" in result.content
    assert len(server.execute_calls) == 1
    execute_payload = server.execute_calls[0]["payload"]
    assert execute_payload["step_id"] == "step-3"
    assert execute_payload["invocation_id"] == "call-xyz"
    assert execute_payload["workspace_id"] == "ws-1"
    assert execute_payload["run_id"] == "run-1"
