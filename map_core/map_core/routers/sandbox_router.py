"""S5-01: deterministic worker->Core sandbox execution endpoint.

The job worker (map-business-backend) calls POST /sandbox/exec when a run
step needs remote OpenSandbox execution. The request MUST carry the
COMPLETE six-field durable identity chain (workspace/run/step/attempt/
invocation/client_request) as headers - a missing field fails closed with
HTTP 400 (OPENSANDBOX_IDENTITY_INCOMPLETE) and nothing is invented locally.
This closes the S5-01 loop: a REAL worker -> Core request carries the six
fields and Core validates every one of them before the sandbox tool runs.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..service.agent.base import AgentRequest
from ..service.opensandbox_client import OpenSandboxClientError
from ..service.sandbox_tools import (
    IDENTITY_INCOMPLETE,
    _sandbox_execute_handler,
)

sandbox_router = APIRouter(prefix="/sandbox", tags=["sandbox"])

# field name -> inbound header carrying it (shared F-04 ID contract: <=128
# chars, charset [A-Za-z0-9._:-]).
IDENTITY_HEADERS: dict[str, str] = {
    "workspace_id": "X-Workspace-ID",
    "run_id": "X-Run-ID",
    "step_id": "X-Step-ID",
    "attempt_id": "X-Attempt-ID",
    "invocation_id": "X-Invocation-ID",
    "client_request_id": "X-Client-Request-ID",
}

_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

INTERNAL_ERROR = "OPENSANDBOX_INTERNAL"


class SandboxExecRequest(BaseModel):
    command: str


@sandbox_router.post("/exec")
async def sandbox_execute(request: Request, payload: SandboxExecRequest) -> JSONResponse:
    command = (payload.command or "").strip()
    if not command:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "command must be a non-empty string",
                "error_code": "OPENSANDBOX_BAD_REQUEST",
            },
        )
    # S5-01: validate ALL six identity fields up front (fail-closed).
    missing: list[str] = []
    extra: dict[str, str] = {}
    for field, header in IDENTITY_HEADERS.items():
        value = (request.headers.get(header) or "").strip()
        if not value or not _ID_RE.fullmatch(value):
            missing.append(field)
        else:
            extra[field] = value
    if missing:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    f"{IDENTITY_INCOMPLETE}: the caller must supply the "
                    "complete durable identity; missing: "
                    + ", ".join(sorted(missing))
                ),
                "error_code": IDENTITY_INCOMPLETE,
            },
        )

    agent_request = AgentRequest(
        query=command,
        staff_code="job-worker",
        extra=extra,
    )
    try:
        result = await _sandbox_execute_handler(
            {"command": command},
            agent_request,
            parid="worker-sandbox-exec",
        )
    except OpenSandboxClientError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "error_code": exc.code},
        )
    except Exception as exc:  # noqa: BLE001 - handler boundary
        return JSONResponse(
            status_code=500,
            content={
                "detail": str(exc)[:2000],
                "error_code": INTERNAL_ERROR,
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "success": result.success,
            "content": result.content,
            "error": result.error,
            "data_source": result.data_source,
        },
    )
