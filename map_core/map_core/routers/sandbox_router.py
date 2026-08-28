"""S5-01 / S6-03: authenticated worker->Core sandbox execution endpoint.

The Run worker (map-business-backend) calls POST /sandbox/exec when a run
step needs remote OpenSandbox execution, and POST /sandbox/reconcile when a
previously-uncertain invocation must be reconciled against the server state.

S6-03 authorization model: the six identity headers are CORRELATION and
IDEMPOTENCY only - they are caller-chosen and can never authorize
anything. A request is authorized ONLY when it presents a Bearer token
matching a deployment-injected credential (constant-time compare) whose
audience equals the configured service audience and whose scopes include
sandbox:execute. Missing/forged/rotated/wrong-audience/wrong-scope
credentials are all rejected with 401/403 BEFORE any remote OpenSandbox
byte; every authorization decision is audit-logged.

Production paths are stateless: ``/sandbox/exec`` and ``/sandbox/reconcile``
call the no-PG ``execute_sandbox_once`` / ``reconcile_sandbox_once``
functions. The legacy ``_sandbox_execute_handler`` + PostgreSQL ledger stay
available for tests and future drain but are NOT called from this router.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from ..service.opensandbox_client import (
    MISSING_CONFIG_ERROR,
    OpenSandboxClient,
    OpenSandboxClientError,
    SandboxIdentity,
    SandboxResourceLimits,
)
from ..service.sandbox_auth import (
    authenticate_sandbox_request,
    parse_sandbox_credentials,
)
from ..service.sandbox_ledger import (
    build_create_key,
    build_execute_key,
    normalize_request_digest,
)
from ..service.sandbox_tools import (
    CAPABILITY_DISABLED,
    IDENTITY_INCOMPLETE,
    SandboxExecOutcome,
    execute_sandbox_once,
    reconcile_sandbox_once,
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
    limits: dict[str, int] | None = None
    create_key: str | None = None
    execute_key: str | None = None
    resume_sandbox_id: str | None = None
    resume_phase: str | None = None


class SandboxReconcileRequest(BaseModel):
    sandbox_id: str
    execute_key: str
    resume_phase: str | None = None
    create_key: str | None = None
    # Identity fields may travel in the request body (the six headers remain
    # the canonical transport, but a body fallback keeps the reconcile seam
    # usable from a stored effect view without reconstructing all headers).
    workspace_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    attempt_id: str | None = None
    invocation_id: str | None = None
    client_request_id: str | None = None


def _credentials_or_fail() -> tuple[object, ...]:
    """Parse the injected credential registry; a broken registry fails
    closed with an internal error (never a widened authorization)."""
    try:
        return parse_sandbox_credentials(os.getenv("MAP_SANDBOX_SERVICE_CREDENTIALS"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"MAP_SANDBOX_SERVICE_CREDENTIALS is malformed: {exc}"
        ) from exc


def _authenticate(request: Request):
    """Return (credential, None) or (None, JSONResponse error)."""
    try:
        credentials = _credentials_or_fail()
    except RuntimeError as exc:
        logger.error("sandbox credentials malformed: {}", exc)
        return None, JSONResponse(
            status_code=500,
            content={"detail": str(exc), "error_code": INTERNAL_ERROR},
        )
    credential, reason = authenticate_sandbox_request(
        request.headers.get("Authorization"), credentials
    )
    if credential is None:
        status_code = 403 if reason == "forbidden" else 401
        logger.info(
            "sandbox authorization REJECTED status={} reason={} request_id={}",
            status_code,
            reason,
            request.headers.get("X-Request-ID"),
        )
        return None, JSONResponse(
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
        "sandbox authorization GRANTED service={} key_id={} request_id={}",
        credential.service_name,
        credential.key_id,
        request.headers.get("X-Request-ID"),
    )
    return credential, None


def _identity_from_headers(
    request: Request,
) -> tuple[SandboxIdentity | None, JSONResponse | None]:
    missing: list[str] = []
    values: dict[str, str] = {}
    for field, header in IDENTITY_HEADERS.items():
        value = (request.headers.get(header) or "").strip()
        if not value or not _ID_RE.fullmatch(value):
            missing.append(field)
        else:
            values[field] = value
    if missing:
        return None, JSONResponse(
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
    return SandboxIdentity(**values), None


def _identity_from_headers_or_body(
    request: Request, payload: SandboxReconcileRequest
) -> tuple[SandboxIdentity | None, JSONResponse | None]:
    headers_identity, headers_error = _identity_from_headers(request)
    if headers_identity is not None:
        return headers_identity, None
    # Body fallback for reconcile: identity travels as explicit fields when
    # the caller reconstructs from a stored effect view.
    values: dict[str, str] = {}
    for field in IDENTITY_HEADERS:
        value = str(getattr(payload, field) or "").strip()
        if value and _ID_RE.fullmatch(value):
            values[field] = value
    if len(values) != len(IDENTITY_HEADERS):
        return None, headers_error
    return SandboxIdentity(**values), None


def _limits_from_body(limits: dict[str, int] | None) -> SandboxResourceLimits:
    defaults = SandboxResourceLimits()
    if not limits:
        return defaults
    valid = {
        key: int(limits[key])
        for key in defaults.__dict__
        if key in limits and isinstance(limits[key], int)
    }
    return SandboxResourceLimits(**valid) if valid else defaults


def _body_from_outcome(outcome: SandboxExecOutcome) -> dict[str, object]:
    data_source: dict[str, object] = {"source": "opensandbox"}
    if outcome.error_code is not None:
        data_source["error_code"] = outcome.error_code
    return {
        "success": outcome.success,
        "content": outcome.output or "",
        "output": outcome.output,
        "error": outcome.error or "",
        "sandbox_id": outcome.sandbox_id,
        "server_state": outcome.server_state,
        "data_source": data_source,
    }


def _client_from_env() -> tuple[OpenSandboxClient | None, JSONResponse | None]:
    try:
        return OpenSandboxClient.from_env(), None
    except OpenSandboxClientError as exc:
        if exc.code == MISSING_CONFIG_ERROR:
            return None, JSONResponse(
                status_code=200,
                content=_body_from_outcome(
                    SandboxExecOutcome(
                        success=False,
                        status="failed",
                        error_code=CAPABILITY_DISABLED,
                        error=f"{CAPABILITY_DISABLED}: {exc}",
                    )
                ),
            )
        return None, JSONResponse(
            status_code=502,
            content={"detail": str(exc), "error_code": exc.code},
        )


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
    credential, auth_error = _authenticate(request)
    if credential is None:
        return auth_error
    identity, identity_error = _identity_from_headers(request)
    if identity is None:
        return identity_error

    limits = _limits_from_body(payload.limits)
    request_digest = normalize_request_digest(
        command=command, limits=limits.to_dict()
    )
    create_key = payload.create_key or build_create_key(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=request_digest,
    )
    execute_key = payload.execute_key or build_execute_key(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=request_digest,
    )

    client, client_error = _client_from_env()
    if client is None:
        return client_error
    try:
        outcome = await execute_sandbox_once(
            command=command,
            identity=identity,
            limits=limits,
            create_key=create_key,
            execute_key=execute_key,
            client=client,
            resume_sandbox_id=payload.resume_sandbox_id,
            resume_phase=payload.resume_phase,
        )
    except OpenSandboxClientError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "error_code": exc.code},
        )
    except Exception as exc:  # noqa: BLE001 - router boundary
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)[:2000], "error_code": INTERNAL_ERROR},
        )
    finally:
        await client.aclose()
    return JSONResponse(status_code=200, content=_body_from_outcome(outcome))


@sandbox_router.post("/reconcile")
async def sandbox_reconcile(
    request: Request, payload: SandboxReconcileRequest
) -> JSONResponse:
    sandbox_id = (payload.sandbox_id or "").strip()
    execute_key = (payload.execute_key or "").strip()
    if not sandbox_id or not execute_key:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "sandbox_id and execute_key must be non-empty strings",
                "error_code": "OPENSANDBOX_BAD_REQUEST",
            },
        )
    credential, auth_error = _authenticate(request)
    if credential is None:
        return auth_error
    identity, identity_error = _identity_from_headers_or_body(request, payload)
    if identity is None:
        return identity_error

    client, client_error = _client_from_env()
    if client is None:
        return client_error
    try:
        outcome = await reconcile_sandbox_once(
            sandbox_id=sandbox_id,
            execute_key=execute_key,
            client=client,
            resume_phase=payload.resume_phase,
        )
    except OpenSandboxClientError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "error_code": exc.code},
        )
    except Exception as exc:  # noqa: BLE001 - router boundary
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)[:2000], "error_code": INTERNAL_ERROR},
        )
    finally:
        await client.aclose()
    return JSONResponse(status_code=200, content=_body_from_outcome(outcome))
