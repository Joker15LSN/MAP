"""S5-01 crash-window acceptance: SIGKILL the owner, verify convergence.

Real processes, real PostgreSQL, a real HTTP OpenSandbox 0.2.2 double with
per-key idempotent dedup (the real server image is pinned in Compose but
unpullable in this environment - ghcr denies the manifest; the AC-SEC-12
real-server run remains gated on the deployment task).

For each crash window the test:

1. spawns a victim process that claims the row and reaches the window
   (after_claim / after_create / after_execute / after_complete);
2. SIGKILLs it exactly at the barrier;
3. runs the REAL handler again with the same six-field identity and
   requires convergence to a definite terminal state within the budget;
4. asserts the server-side create/execute ACTION counts stayed <= 1
   (idempotency-key dedup), the row reached succeeded, and the digest
   conflict rule still holds.

Skipped (like the other PG tests) when PostgreSQL is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from map_core.service.agent.base import AgentRequest
from map_core.service.opensandbox_client import (
    IDEMPOTENCY_HEADER,
    OpenSandboxClient,
    SandboxIdentity,
    SandboxResourceLimits,
)
from map_core.service.sandbox_ledger import (
    STATUS_SUCCEEDED,
    PostgresSandboxInvocationLedger,
)
from map_core.service.sandbox_tools import _sandbox_execute_handler

HELPER = Path(__file__).parent / "sandbox_crash_helper.py"
DSN = os.getenv("POSTGRES_DSN", "postgresql://map:map@127.0.0.1:15432/map")


class OpenSandboxDoubleHandler(BaseHTTPRequestHandler):
    server_state: dict = {}  # class-level, shared per test server

    def log_message(self, *args) -> None:  # noqa: ANN002 - silence
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        with self.server.lock:
            state = self.server_state
            key = self.headers.get(IDEMPOTENCY_HEADER)
            if self.path == "/api/v1/sandboxes":
                payload = self._read()
                workspace_id = payload.get("workspace_id")
                if key and key in state["creates"]:
                    sandbox_id = state["creates"][key]
                    return self._json(201, {"sandbox_id": sandbox_id, "status": "ready"})
                sandbox_id = f"sb-{state['sandbox_seq']}"
                state["sandbox_seq"] += 1
                state["create_actions"] += 1
                if key:
                    state["creates"][key] = sandbox_id
                state["sandboxes"][workspace_id] = {
                    "sandbox_id": sandbox_id,
                    "status": "ready",
                    "executions": [],
                }
                return self._json(201, {"sandbox_id": sandbox_id, "status": "ready"})
            if self.path.endswith("/execute"):
                sandbox_id = self.path.rsplit("/", 2)[-2]
                payload = self._read()
                sandbox = state["sandboxes"].get(payload.get("workspace_id"))
                if sandbox is None or sandbox["sandbox_id"] != sandbox_id:
                    return self._json(404, {"error": "unknown sandbox"})
                for execution in sandbox["executions"]:
                    if execution.get("key") == key:
                        return self._json(
                            200,
                            {
                                "sandbox_id": sandbox_id,
                                "status": "completed",
                                "exit_code": 0,
                                "output": execution["output"],
                            },
                        )
                execution = {
                    "key": key,
                    "command": payload.get("command"),
                    "output": f"ok: {payload.get('command')}",
                }
                sandbox["executions"].append(execution)
                state["execute_actions"] += 1
                return self._json(
                    200,
                    {
                        "sandbox_id": sandbox_id,
                        "status": "completed",
                        "exit_code": 0,
                        "output": execution["output"],
                    },
                )
            return self._json(404, {"error": "no route"})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        with self.server.lock:
            if "/api/v1/sandboxes/" in self.path:
                sandbox_id = self.path.rsplit("/", 1)[-1]
                for sandbox in self.server_state["sandboxes"].values():
                    if sandbox["sandbox_id"] == sandbox_id:
                        return self._json(200, sandbox)
            return self._json(404, {"error": "unknown sandbox"})

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        with self.server.lock:
            sandbox_id = self.path.rsplit("/", 1)[-1]
            for ws, sandbox in list(self.server_state["sandboxes"].items()):
                if sandbox["sandbox_id"] == sandbox_id:
                    del self.server_state["sandboxes"][ws]
                    self.server_state["destroy_actions"] += 1
            self.send_response(204)
            self.end_headers()


class OpenSandboxDouble:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), OpenSandboxDoubleHandler)
        self.server.lock = threading.Lock()
        self.server_state = {
            "sandbox_seq": 1,
            "create_actions": 0,
            "execute_actions": 0,
            "destroy_actions": 0,
            "creates": {},
            "sandboxes": {},
        }
        OpenSandboxDoubleHandler.server_state = self.server_state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _wait_for_barrier(path: str, marker: str, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if Path(path).read_text(encoding="utf-8").strip() == marker:
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise RuntimeError(f"victim never reached {marker}")


def _run_handler(identity: SandboxIdentity, command: str) -> object:
    from map_core.service import sandbox_tools as st

    request = AgentRequest(
        query=command, staff_code="pytest", extra=identity.to_dict()
    )

    async def run() -> object:
        try:
            return await _sandbox_execute_handler(
                {"command": command}, request, "parid"
            )
        finally:
            # Each asyncio.run gets a fresh loop; the module-global ledger
            # pool must not leak across closed loops.
            await st.close_sandbox_ledger()

    return asyncio.run(run())


def _check_pg_available() -> None:
    async def probe() -> None:
        ledger = PostgresSandboxInvocationLedger(DSN)
        try:
            await ledger.get(workspace_id="ws-probe", invocation_id="inv-probe")
        finally:
            await ledger.close()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001 - optional local PG
        pytest.skip(f"postgres ledger unavailable: {exc}")


@pytest.mark.parametrize(
    "window",
    ["after_claim", "after_create", "after_execute", "after_complete"],
)
def test_sigkill_window_converges(window: str, monkeypatch, tmp_path) -> None:
    _check_pg_available()
    double = OpenSandboxDouble()
    double.start()
    try:
        monkeypatch.setenv("MAP_OPENSANDBOX_URL", double.url)
        monkeypatch.setenv("MAP_OPENSANDBOX_API_KEY", "key-1234567890abcdef")
        monkeypatch.setenv("POSTGRES_DSN", DSN)
        # Small lease so the takeover happens within the test budget.
        monkeypatch.setenv("MAP_SANDBOX_LEASE_SECONDS", "0.4")
        monkeypatch.setenv("MAP_SANDBOX_IN_PROGRESS_WAIT_SECONDS", "30")

        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-{suffix}"
        invocation_id = f"inv-{suffix}"
        barrier = str(tmp_path / "barrier.txt")
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1])
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
            "POSTGRES_DSN": DSN,
            "MAP_OPENSANDBOX_URL": double.url,
            "MAP_OPENSANDBOX_API_KEY": "key-1234567890abcdef",
            "MAP_CRASH_WINDOW": window,
            "MAP_CRASH_BARRIER": barrier,
            "MAP_CRASH_WORKSPACE": workspace_id,
            "MAP_CRASH_RUN": f"run-{suffix}",
            "MAP_CRASH_INVOCATION": invocation_id,
            "MAP_CRASH_COMMAND": "echo crash-window",
            "MAP_CRASH_LEASE": "0.4",
        }
        victim = subprocess.Popen(
            [sys.executable, str(HELPER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_barrier(barrier, window, timeout_s=30.0)
            victim.send_signal(signal.SIGKILL)
            victim.wait(timeout=10.0)

            identity = SandboxIdentity(
                workspace_id=workspace_id,
                run_id=f"run-{suffix}",
                step_id="step-1",
                attempt_id="att-1",
                invocation_id=invocation_id,
                client_request_id=f"req-{suffix}",
            )
            started = time.monotonic()
            result = _run_handler(identity, "echo crash-window")
            elapsed = time.monotonic() - started
            assert elapsed < 30.0, f"convergence too slow after {window}: {elapsed:.1f}s"

            if window == "after_complete":
                # The victim already wrote the terminal state: the retry
                # replays it without any new server action.
                assert result.success is True
            else:
                assert result.success is True, (
                    f"window {window} did not converge: {result.error}"
                )
                assert "ok: echo crash-window" in result.content

            with double.server.lock:
                create_actions = double.server_state["create_actions"]
                execute_actions = double.server_state["execute_actions"]
            assert create_actions <= 1, f"create actions {create_actions} > 1"
            assert execute_actions <= 1, f"execute actions {execute_actions} > 1"
            assert double.server_state["sandbox_seq"] <= 2

            async def read_row() -> None:
                ledger = PostgresSandboxInvocationLedger(DSN)
                try:
                    record = await ledger.get(
                        workspace_id=workspace_id, invocation_id=invocation_id
                    )
                finally:
                    await ledger.close()
                assert record is not None, "no durable row after convergence"
                assert record.terminal, f"row not terminal after {window}"
                assert record.status == STATUS_SUCCEEDED
                assert record.output == "ok: echo crash-window"

            asyncio.run(read_row())

            # Digest conflict rule still holds after the crash windows.
            conflict = _run_handler(identity, "echo DIFFERENT")
            assert conflict.success is False
            assert "OPENSANDBOX_IDEMPOTENCY_CONFLICT" in conflict.error
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait(timeout=10.0)
    finally:
        double.stop()
