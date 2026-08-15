"""S5-01: the real worker -> Core request carries the six-field identity.

Runs the REAL sandbox_exec worker handler through the REAL JobRunner
(claim/heartbeat/fenced complete on PostgreSQL) against a real HTTP Core
double that validates the six durable identity headers exactly like the
production /sandbox/exec endpoint (missing field = 400 fail-closed). The
job must reach succeeded with the Core double having observed all six
fields.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Job, JobStatus
from app.workers.job_runner import JobRunner
from app.workers.main import _sandbox_exec_handler

pytestmark = pytest.mark.asyncio

WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
IDENTITY_HEADERS = (
    "x-workspace-id",
    "x-run-id",
    "x-step-id",
    "x-attempt-id",
    "x-invocation-id",
    "x-client-request-id",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeCoreServer:
    """Real HTTP double of map_core's POST /sandbox/exec boundary."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        app = FastAPI()
        self.app = app
        self.port = _free_port()

        @app.post("/sandbox/exec")
        async def sandbox_exec(request: Request):
            headers = {name: request.headers.get(name, "") for name in IDENTITY_HEADERS}
            missing = [name for name, value in headers.items() if not value.strip()]
            if missing:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"missing identity: {missing}"},
                )
            self.received.append(headers)
            return {
                "success": True,
                "content": "ok",
                "error": None,
                "data_source": {"source": "opensandbox"},
            }

    def serve(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.server.run()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def _create_job(session, *, payload, max_attempts=3):
    job = Job(
        workspace_id=WORKSPACE_ID,
        job_type="sandbox_exec",
        payload_json=payload,
        idempotency_key=f"sandbox-key-{uuid4().hex[:8]}",
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def _clear_jobs(session) -> None:
    """Isolate the test from leftover queued jobs of earlier runs: the
    runner claims the OLDEST queued job, so a stale row would steal the
    claim and hide this test's job."""
    from sqlalchemy import delete

    await session.execute(delete(Job))
    await session.commit()


async def test_worker_sandbox_exec_carries_complete_identity(_engine, monkeypatch) -> None:
    fake_core = FakeCoreServer()
    thread = threading.Thread(target=fake_core.serve, daemon=True)
    thread.start()
    try:
        # Wait for the server to accept connections.
        deadline = asyncio.get_running_loop().time() + 15.0
        while True:
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", fake_core.port)
                writer.close()
                break
            except OSError as exc:
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("fake core server did not start") from exc
                await asyncio.sleep(0.1)

        monkeypatch.setenv("MAP_CORE_API_ORIGIN", fake_core.url)
        factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            await _clear_jobs(s)
            job = await _create_job(s, payload={"command": "echo hi"})

        runner = JobRunner(
            factory,
            {"sandbox_exec": _sandbox_exec_handler},
            worker_id="sandbox-worker",
            poll_seconds=0.01,
        )
        assert await runner.run_once() is True

        async with factory() as s:
            stored = await s.get(Job, job.id)
            assert stored.status == JobStatus.SUCCEEDED
            assert (stored.result_json or {}).get("sandbox", {}).get("success") is True

        assert len(fake_core.received) == 1
        seen = fake_core.received[0]
        for name in IDENTITY_HEADERS:
            assert seen.get(name), f"Core request missing {name}"
        # The worker owns run/attempt/client_request; step/invocation are
        # minted per job attempt; workspace is the job's workspace.
        assert seen["x-workspace-id"] == WORKSPACE_ID
        assert seen["x-run-id"] == str(job.id)
        # The claim increments the attempt; the header carries the attempt
        # the RUNNER held while executing (1 for the first claim).
        assert seen["x-attempt-id"] == f"att-{stored.attempt}"
        assert seen["x-client-request-id"] == job.idempotency_key
        assert seen["x-step-id"].startswith("step-")
        assert seen["x-invocation-id"].startswith("inv-")
    finally:
        fake_core.server.should_exit = True


async def test_worker_sandbox_exec_missing_command_fails_job(_engine, monkeypatch) -> None:
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await _clear_jobs(s)
        job = await _create_job(s, payload={"command": "  "}, max_attempts=1)

    runner = JobRunner(
        factory,
        {"sandbox_exec": _sandbox_exec_handler},
        worker_id="sandbox-worker",
        poll_seconds=0.01,
    )
    assert await runner.run_once() is True
    async with factory() as s:
        stored = await s.get(Job, job.id)
        assert stored.status == JobStatus.FAILED
        assert stored.error_code == "HANDLER_ERROR"
        assert "non-empty command" in (stored.error_message or "")
