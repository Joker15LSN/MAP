"""Step 8 PR-K5: service-identity NDJSON execution event stream.

These tests drive the REAL FastAPI router with a scripted
:class:`GlobalDomain` fake so no model is called.  The fake still emits
through the REAL :class:`ExecutionEventEmitter` / NDJSON sink protocol,
which keeps the wire contract under test: Bearer auth fail-closed,
``application/x-ndjson``, one ``CoreExecutionEvent`` per line in seq
order, terminal last, and no legacy SSE frames anywhere in the body.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from map_core.routers.execution_router import execution_router
from map_core.service.execution_event import ExecutionEventEmitter

TOKEN = "run-svc-token"
WORKSPACE_ID = "5a0ffc7e-9e9c-4a59-a3b4-7dffcd00e91a"


def _credential(**overrides) -> dict:
    entry = {
        "key_id": "k-run",
        "token": TOKEN,
        "service_name": "map-bff",
        "audience": "map-core",
        "scopes": ["runs.execute"],
        "expires_at": "2099-12-31T23:59:59Z",
    }
    entry.update(overrides)
    return entry


def _credentials(entries: list[dict] | None = None) -> str:
    return json.dumps(entries or [_credential()])


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(execution_router)
    return app


@pytest.fixture(autouse=True)
def _run_credentials(monkeypatch):
    monkeypatch.setenv("MAP_RUN_SERVICE_AUDIENCE", "map-core")
    monkeypatch.setenv("MAP_RUN_SERVICE_CREDENTIALS", _credentials())


def _headers(run_id: uuid.UUID, attempt: int = 1, **overrides) -> dict:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Service-Name": "map-bff",
        "X-Service-Audience": "map-core",
        "X-Workspace-ID": WORKSPACE_ID,
        "X-Request-ID": str(run_id),
    }
    headers.update(overrides)
    return headers


def _url(run_id: uuid.UUID, attempt: int = 1) -> str:
    return f"/internal/v1/runs/{run_id}/attempts/{attempt}/events"


def _body() -> dict:
    return {
        "query": "hello",
        "chart_plotting_enabled": False,
        "content_review_enabled": False,
    }


class ScriptedGlobalDomain:
    """Fake GlobalDomain: emits typed events through the real emitter and
    yields discarded stand-ins for the legacy SSE frames."""

    def __init__(self, request=None, http_request=None) -> None:
        self.request = request
        self.http_request = http_request
        self.run_id: str | None = None
        self.attempt_id: str | None = None
        self.client_request_id: str | None = None

    def pipeline_stream(self, request):
        async def _gen():
            emitter = ExecutionEventEmitter.current()
            emitter.emit("step.started", data={"step_id": "s-1"})
            yield "discarded:start"
            emitter.emit("message.delta", data={"content": "hel"})
            emitter.emit("message.delta", data={"content": "lo"})
            yield "discarded:content_delta"
            emitter.emit("checkpoint.written", data={"phase": "request.end"})
            yield "discarded:done"

        return _gen()


class FailingScriptedGlobalDomain(ScriptedGlobalDomain):
    def pipeline_stream(self, request):
        async def _gen():
            ExecutionEventEmitter.current().emit("step.started", data={})
            yield "discarded:start"
            raise RuntimeError("scripted core execution failure")

        return _gen()


@pytest.fixture()
def scripted(monkeypatch):
    from map_core.routers import execution_router as router

    monkeypatch.setattr(router, "GlobalDomain", ScriptedGlobalDomain)
    return router


def _run_post(
    *,
    monkeypatch=None,
    run_id: uuid.UUID,
    json_body: dict | None = None,
    headers: dict | None = None,
    global_domain_class=ScriptedGlobalDomain,
) -> httpx.Response:
    from map_core.routers import execution_router as router

    if monkeypatch is not None:
        monkeypatch.setattr(router, "GlobalDomain", global_domain_class)

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            if json_body is None:
                return await client.post(_url(run_id), headers=headers or _headers(run_id))
            return await client.post(
                _url(run_id),
                json=json_body,
                headers=headers or _headers(run_id),
            )

    return asyncio.run(_run())


def test_missing_credentials_401(scripted, monkeypatch) -> None:
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id, Authorization=""),
    )
    assert response.status_code == 401


def test_wrong_audience_403(scripted, monkeypatch) -> None:
    monkeypatch.setenv(
        "MAP_RUN_SERVICE_CREDENTIALS",
        _credentials([_credential(audience="other-service")]),
    )
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id),
    )
    assert response.status_code == 403


def test_wrong_scope_403(scripted, monkeypatch) -> None:
    monkeypatch.setenv(
        "MAP_RUN_SERVICE_CREDENTIALS",
        _credentials([_credential(scopes=[])]),
    )
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id),
    )
    assert response.status_code == 403


def test_expired_credential_401(scripted, monkeypatch) -> None:
    expired = _credential(
        expires_at=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
    )
    monkeypatch.setenv("MAP_RUN_SERVICE_CREDENTIALS", _credentials([expired]))
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id),
    )
    assert response.status_code == 401


def test_valid_credential_200_ndjson_seq_increasing(scripted, monkeypatch) -> None:
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    lines = [line.strip() for line in response.text.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    assert len(events) >= 2
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events[:-1]] == [
        "step.started",
        "message.delta",
        "message.delta",
        "checkpoint.written",
    ]
    assert events[-1]["type"] == "stream.terminal"
    assert events[-1]["data"] == {
        "status": "completed",
        "error_code": None,
        "error_message": None,
    }
    # The typed stream must never leak legacy SSE frame names.
    legacy_frames = {"start", "content_delta", "meta", "done", "error"}
    assert not (legacy_frames & {event["type"] for event in events})
    for event in events:
        assert event["run_id"] == str(run_id)
        assert event["attempt"] == 1
        assert event["workspace_id"] == WORKSPACE_ID


def test_execution_exception_terminal_failed_http_200(scripted, monkeypatch) -> None:
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id),
        global_domain_class=FailingScriptedGlobalDomain,
    )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[-1]["type"] == "stream.terminal"
    assert events[-1]["data"]["status"] == "failed"
    assert events[-1]["data"]["error_code"] == "EXECUTION_FAILED"
    assert "scripted core execution failure" in events[-1]["data"]["error_message"]


def test_empty_body_400(scripted, monkeypatch) -> None:
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        headers=_headers(run_id),
    )
    assert response.status_code == 400


def test_run_id_header_mismatch_400(scripted, monkeypatch) -> None:
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id, **{"X-Run-ID": str(uuid.uuid4())}),
    )
    assert response.status_code == 400


def test_attempt_header_mismatch_400(scripted, monkeypatch) -> None:
    run_id = uuid.uuid4()
    response = _run_post(
        monkeypatch=monkeypatch,
        run_id=run_id,
        json_body=_body(),
        headers=_headers(run_id, **{"X-Attempt-ID": "att-9"}),
    )
    assert response.status_code == 400
