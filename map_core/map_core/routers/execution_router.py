"""Step 8 PR-K5: service-identity NDJSON typed execution event stream.

``POST /internal/v1/runs/{run_id}/attempts/{attempt}/events`` is the
worker->Core seam that replaces the legacy SSE chat stream for Run
attempts.  Authorization comes exclusively from the deployment-injected
``MAP_RUN_SERVICE_CREDENTIALS`` registry (Bearer token, constant-time
match, audience + ``runs.execute`` scope, temporal window).  The route
body is the existing :class:`GlobalDomainChatSchema`; the response is
``application/x-ndjson`` with one :class:`CoreExecutionEvent` per line and
a final ``stream.terminal`` line.

The legacy SSE frames (``start/content_delta/meta/done/error``) are NEVER
written to this response: the pipeline generator is consumed only for its
typed-emitter side effects and its SSE frames are discarded.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import ValidationError

from ..schema.global_domain_schema import GlobalDomainChatSchema
from ..service.execution_event import (
    ExecutionEventEmitter,
    NdjsonExecutionEventSink,
    coerce_uuid,
    set_run_context,
)
from ..service.global_domain import GlobalDomain
from ..service.run_auth import (
    authenticate_run_request,
    parse_run_credentials,
)
from ._request_context import build_service_run_context

execution_router = APIRouter(prefix="/internal/v1", tags=["internal"])

_RUNS_EXECUTE_ERROR = "RUNS_EXECUTE_UNAUTHORIZED"
_RUNS_EXECUTE_FORBIDDEN = "RUNS_EXECUTE_FORBIDDEN"
_RUNS_EXECUTE_INVALID = "RUNS_EXECUTE_INVALID_REQUEST"

_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_ATTEMPT_RE = re.compile(r"^att-(\d+)$")


def _validated_id_header(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value and _ID_RE.fullmatch(value):
        return value
    return None


def _credentials_or_fail() -> tuple[object, ...]:
    try:
        return parse_run_credentials(os.getenv("MAP_RUN_SERVICE_CREDENTIALS"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"MAP_RUN_SERVICE_CREDENTIALS is malformed: {exc}"
        ) from exc


def _authenticate(request: Request) -> JSONResponse | None:
    """Authorize the request; return an error response when rejected."""
    try:
        credentials = _credentials_or_fail()
    except RuntimeError as exc:
        logger.error("run service credentials malformed: {}", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "error_code": _RUNS_EXECUTE_INVALID},
        )
    credential, reason = authenticate_run_request(
        request.headers.get("Authorization"), credentials
    )
    if credential is None:
        status_code = 403 if reason == "forbidden" else 401
        logger.info(
            "run stream authorization REJECTED status={} reason={} request_id={}",
            status_code,
            reason,
            request.headers.get("X-Request-ID"),
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": (
                    "the typed run stream requires a valid service credential "
                    "with the runs.execute scope (fail-closed)"
                ),
                "error_code": (
                    _RUNS_EXECUTE_FORBIDDEN
                    if status_code == 403
                    else _RUNS_EXECUTE_ERROR
                ),
            },
        )
    logger.info(
        "run stream authorization GRANTED service={} key_id={} request_id={}",
        credential.service_name,
        credential.key_id,
        request.headers.get("X-Request-ID"),
    )
    return None


def _parse_attempt_header(raw: str | None) -> int | None:
    """Parse an X-Attempt-ID header into an attempt number, or None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value.isdigit() and int(value) >= 1:
        return int(value)
    match = _ATTEMPT_RE.fullmatch(value)
    if match:
        return int(match.group(1))
    return None


def _parse_path_attempt(raw: str) -> int | None:
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value >= 1 else None


def _invalid_request(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": detail, "error_code": _RUNS_EXECUTE_INVALID},
    )


def _validate_identity_consistency(
    request: Request,
    *,
    run_id: uuid.UUID,
    attempt: int,
) -> JSONResponse | None:
    """Fail closed when caller-chosen identity headers contradict the path."""
    header_run_id = request.headers.get("X-Run-ID")
    if header_run_id is not None:
        try:
            parsed_run_id = uuid.UUID(header_run_id.strip())
        except (ValueError, AttributeError):
            return _invalid_request("X-Run-ID must be a valid UUID")
        if parsed_run_id != run_id:
            return _invalid_request(
                "X-Run-ID header does not match the path run_id"
            )
    header_attempt_id = request.headers.get("X-Attempt-ID")
    if header_attempt_id is not None:
        parsed_attempt = _parse_attempt_header(header_attempt_id)
        if parsed_attempt is None:
            return _invalid_request(
                "X-Attempt-ID must be an attempt number or att-N"
            )
        if parsed_attempt != attempt:
            return _invalid_request(
                "X-Attempt-ID header does not match the path attempt"
            )
    return None


def _parse_run_identity(
    request: Request,
    *,
    run_id: str,
    attempt: str,
) -> tuple[uuid.UUID | None, int | None, JSONResponse | None]:
    """Validate path identity and header/path consistency.

    Returns ``(run_id, attempt, None)`` on success, or ``(None, None,
    error_response)`` on any fail-closed validation failure.
    """
    try:
        path_run_id = uuid.UUID(run_id)
    except (ValueError, AttributeError):
        return None, None, _invalid_request(
            "run_id path parameter must be a valid UUID"
        )
    path_attempt = _parse_path_attempt(attempt)
    if path_attempt is None:
        return None, None, _invalid_request(
            "attempt path parameter must be an integer >= 1"
        )
    identity_error = _validate_identity_consistency(
        request,
        run_id=path_run_id,
        attempt=path_attempt,
    )
    if identity_error is not None:
        return None, None, identity_error
    return path_run_id, path_attempt, None


async def _parse_chat_schema(
    http_request: Request,
) -> tuple[GlobalDomainChatSchema | None, JSONResponse | None]:
    try:
        raw_body = await http_request.body()
    except Exception as exc:  # noqa: BLE001 - transport read failure
        return None, _invalid_request(f"failed to read request body: {exc}")
    if not raw_body:
        return None, _invalid_request("request body must be a JSON object")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return None, _invalid_request("request body must be valid JSON")
    if not isinstance(payload, dict):
        return None, _invalid_request("request body must be a JSON object")
    try:
        return GlobalDomainChatSchema.model_validate(payload), None
    except ValidationError as exc:
        return None, _invalid_request(f"invalid GlobalDomainChatSchema: {exc}")


def _terminal_data(
    status: str,
    *,
    error_code: str | None,
    error_message: str | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
    }


async def _produce_events(
    emitter: ExecutionEventEmitter,
    event_stream: Any,
    run_context: Any,
) -> None:
    """Consume the real GlobalDomain stream and close the emitter.

    The SSE frames yielded by ``event_stream`` are discarded; the typed
    emitter already produced the durable NDJSON facts.  A raised execution
    error becomes ``stream.terminal`` with ``status="failed"`` while the
    HTTP response remains 200 (it is a core verdict, not a transport
    failure).
    """
    try:
        with set_run_context(
            run_id=run_context.run_id,
            workspace_id=run_context.workspace_id,
            attempt=run_context.attempt,
            request_id=run_context.request_id,
            session_id=run_context.session_id,
            staff_code=run_context.staff_code,
        ):
            async for _ in event_stream:
                pass
    except Exception as exc:  # noqa: BLE001 - core verdict, not transport
        logger.exception("Global domain execution stream failed")
        terminal = _terminal_data(
            "failed",
            error_code="EXECUTION_FAILED",
            error_message=str(exc),
        )
    else:
        terminal = _terminal_data(
            "completed",
            error_code=None,
            error_message=None,
        )
    emitter.emit("stream.terminal", data=terminal)
    # drain before close so every event (terminal last) is written to the
    # NDJSON queue in seq order; close then appends the EOF sentinel and
    # releases the emitter registry entry.
    await emitter.drain()
    await emitter.close()


async def _iter_ndjson_lines(run_context: Any, event_stream: Any):
    """Yield NDJSON lines for one attempt until the emitter closes."""
    sink = NdjsonExecutionEventSink()
    emitter = ExecutionEventEmitter.for_context(run_context)
    emitter.attach_sink(sink)
    producer = asyncio.create_task(
        _produce_events(emitter, event_stream, run_context)
    )
    try:
        while True:
            line = await sink.readline()
            if line is None:
                break
            yield line
    finally:
        if not producer.done():
            producer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await producer
        # Fail-closed cleanup: if the producer crashed before closing
        # (e.g. emitter close failure), release the registry entry.
        if not emitter._closed:  # noqa: SLF001 - same-package cleanup guard
            with suppress(Exception):
                await emitter.close()


@execution_router.post(
    "/runs/{run_id}/attempts/{attempt}/events",
    response_class=StreamingResponse,
)
async def execution_event_stream(
    run_id: str,
    attempt: str,
    http_request: Request,
):
    auth_error = _authenticate(http_request)
    if auth_error is not None:
        return auth_error

    path_run_id, path_attempt, identity_error = _parse_run_identity(
        http_request,
        run_id=run_id,
        attempt=attempt,
    )
    if identity_error is not None:
        return identity_error

    chat_request, body_error = await _parse_chat_schema(http_request)
    if body_error is not None:
        return body_error

    # Internal service boundary: freeze the durable identity from the path,
    # not from caller-chosen identity headers.  request_id/session_id stay
    # correlation-only F-04 headers (invalid/missing -> deterministic fallback).
    request_id = _validated_id_header(
        http_request.headers.get("X-Request-ID")
    ) or str(path_run_id)
    session_id = _validated_id_header(http_request.headers.get("X-Session-ID"))
    workspace_id = coerce_uuid(
        _validated_id_header(http_request.headers.get("X-Workspace-ID")),
        namespace="workspace",
    )

    # The GlobalDomain constructor reads http_request.state for its own
    # correlation fields; mirror what _apply_runtime_headers does for the
    # legacy chat routes so the service stream observes one identity.
    http_request.state.request_id = request_id
    http_request.state.session_id = session_id
    http_request.state.workspace_id = _validated_id_header(
        http_request.headers.get("X-Workspace-ID")
    )
    http_request.state.run_id = str(path_run_id)
    http_request.state.attempt_id = f"att-{path_attempt}"
    http_request.state.client_request_id = str(path_run_id)
    http_request.state.request_token = http_request.headers.get("X-Request-Token")

    run_context = build_service_run_context(
        run_id=path_run_id,
        attempt=path_attempt,
        workspace_id=workspace_id,
        request_id=request_id,
        session_id=session_id,
        staff_code=getattr(chat_request, "staff_code", None),
    )

    global_domain = GlobalDomain(request=chat_request, http_request=http_request)
    # Freeze the path identity over GlobalDomain's header-derived defaults so
    # its own state_id / data payloads never drift from the typed RunContext.
    global_domain.run_id = str(path_run_id)
    global_domain.attempt_id = f"att-{path_attempt}"
    global_domain.client_request_id = str(path_run_id)
    event_stream = global_domain.pipeline_stream(chat_request)

    return StreamingResponse(
        _iter_ndjson_lines(run_context, event_stream),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
