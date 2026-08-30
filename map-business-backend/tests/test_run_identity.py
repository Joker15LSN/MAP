"""S4-01: BFF freezes and forwards the durable run identity.

The BFF owns run/attempt/client_request (frozen in middleware and forwarded
to map_core); the run worker's JobExecutionContext can build the six-field
identity a Core tool invocation needs.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import AdminState
from app.workers.job_runner import JobExecutionContext


class _FakeStore:
    async def load(self) -> AdminState:
        return AdminState.default()


app = create_app(store=_FakeStore())
core_client = app.state.core_client
client = TestClient(app)


def test_chat_forwards_frozen_run_identity(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_chat(payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        captured["headers"] = headers
        return {"content": "ok", "meta": {}}

    monkeypatch.setattr(core_client, "chat", fake_chat)

    response = client.post(
        "/api/chat",
        json={"query": "hi"},
        headers={
            "X-Run-ID": "run-9",
            "X-Attempt-ID": "att-2",
            "X-Client-Request-ID": "creq-3",
        },
    )

    assert response.status_code == 200
    assert captured["headers"]["X-Run-ID"] == "run-9"
    assert captured["headers"]["X-Attempt-ID"] == "att-2"
    assert captured["headers"]["X-Client-Request-ID"] == "creq-3"


def test_job_context_builds_six_field_identity() -> None:
    ctx = JobExecutionContext(
        job_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        worker_id="w-1",
        attempt=2,
        lease_expires_at=None,
        idempotency_key="job-key-1",
        run_id=str(uuid.uuid4()),
        client_request_id="creq-1",
        lease_lost=asyncio.Event(),
        cancel=asyncio.Event(),
    )
    extra = ctx.sandbox_identity_extra(step_id="step-3", invocation_id="call-x")
    assert extra["workspace_id"] == str(ctx.workspace_id)
    assert extra["run_id"] == ctx.run_id
    assert extra["step_id"] == "step-3"
    assert extra["attempt_id"] == "att-2"
    assert extra["invocation_id"] == "call-x"
    assert extra["client_request_id"] == "creq-1"
