"""S5-01 / S6-03: authenticated worker->Core sandbox execution endpoint.

The job worker (map-business-backend) calls POST /sandbox/exec when a run
step needs remote OpenSandbox execution.

S6-03 authorization model: the six identity headers are CORRELATION and
IDEMPOTENCY only - they are caller-chosen and can never authorize
anything. A request is authorized ONLY when it presents a Bearer token
matching a deployment-injected credential (constant-time compare) whose
audience equals the configured service audience and whose scopes include
sandbox:execute. Missing/forged/rotated/wrong-audience/wrong-scope
credentials are all rejected with 401/403 BEFORE any ledger write or any
remote OpenSandbox byte; every authorization decision is audit-logged.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from ..service.agent.base import AgentRequest
from ..service.opensandbox_client import OpenSandboxClientError
from ..service.sandbox_auth import (
    authenticate_sandbox_request,
    parse_sandbox_credentials,
)
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
AUTH_ERROR = "OPENSANDBOX_UNAUTHORIZED"
FORBIDDEN_ERROR = "OPENSANDBOX_FORBIDDEN"


class SandboxExecRequest(BaseModel):
    command: str


def _credentials_or_fail() -> tuple[object, ...]:
    """Parse the injected credential registry; a broken registry fails
    closed with an internal error (never a widened authorization)."""
    import os

    try:
        return parse_sandbox_credentials(os.getenv("MAP_SANDBOX_SERVICE_CREDENTIALS"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"MAP_SANDBOX_SERVICE_CREDENTIALS is malformed: {exc}"
        ) from exc


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
    # S6-03: authorization FIRST - the six identity fields are correlation
    # only. Missing/forged/wrong-audience tokens are all 401; a valid
    # principal without the sandbox:execute scope is 403. Nothing reaches
    # the ledger or OpenSandbox until a credential is authorized.
    try:
        credentials = _credentials_or_fail()
    except RuntimeError as exc:
        logger.error("sandbox exec credentials malformed: {}", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "error_code": INTERNAL_ERROR},
        )
    credential, reason = authenticate_sandbox_request(
        request.headers.get("Authorization"), credentials
    )
    if credential is None:
        # 401 when nothing matched at all (missing/forged/rotated);
        # 403 when a REGISTERED token matched but its audience or scope is
        # wrong. Both reject before any ledger write or remote byte.
        status_code = 403 if reason == "forbidden" else 401
        logger.info(
            "sandbox exec authorization REJECTED status={} reason={} request_id={}",
            status_code,
            reason,
            request.headers.get("X-Request-ID"),
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": (
                    "sandbox execution requires a valid service credential "
                    "with the sandbox:execute scope (fail-closed)"
                ),
                "error_code": FORBIDDEN_ERROR if status_code == 403 else AUTH_ERROR,
            },
        )
    logger.info(
        "sandbox exec authorization GRANTED service={} key_id={} request_id={}",
        credential.service_name,
        credential.key_id,
        request.headers.get("X-Request-ID"),
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
        staff_code=credential.service_name,
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
