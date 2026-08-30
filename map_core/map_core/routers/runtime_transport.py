"""Step 9 candidate 1: Canonical Run transport adapter (minimal design A).

The seven public names in this module are the only transport seams for the
core routers.  Every helper that is not part of that seam is private.

F-04 id validation stays a single source in
:mod:`map_core.service.run_identity`; this module only delegates to it.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..service.execution_event import RunContext, coerce_uuid, set_run_context
from ..service.run_identity import _valid_id, resolve_run_identity

__all__ = [
    "validated_id_header",
    "parse_attempt",
    "apply_runtime_headers",
    "request_run_context",
    "build_service_run_context",
    "format_sse_event",
    "project_error_response",
]

_ATTEMPT_RE = re.compile(r"^att-(\d+)$")


def validated_id_header(raw: str | None) -> str | None:
    """Return the trimmed header value when it satisfies the F-04 ID contract.

    Contract: non-empty, at most 128 chars, charset [A-Za-z0-9._:-].
    Missing, empty, over-long or out-of-charset values return None.
    """
    return _valid_id(raw)


def parse_attempt(raw: Any) -> int | None:
    """Parse an attempt number from an int >= 1, digit string, or ``att-N``."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 1:
        return raw
    if isinstance(raw, str):
        value = raw.strip()
        if value.isdigit() and int(value) >= 1:
            return int(value)
        match = _ATTEMPT_RE.fullmatch(value)
        if match:
            return int(match.group(1))
    return None


def apply_runtime_headers(
    http_request: Request,
    *,
    request_token: str | None,
) -> None:
    """Freeze the request-boundary identity headers on ``http_request.state``.

    state fields: request_token/x_userid/x_username/request_id/session_id/
    workspace_id/run_id/attempt_id/client_request_id.  The run identity
    (run_id/attempt_id/client_request_id) is resolved by
    :func:`map_core.service.run_identity.resolve_run_identity`.
    """
    http_request.state.request_token = request_token
    http_request.state.x_userid = http_request.headers.get("X-UserId", "missing")
    http_request.state.x_username = http_request.headers.get("X-UserName", "missing")
    # F-04 unified id resolution: honor valid inbound headers, otherwise
    # request_id falls back to a fresh uuid4().hex; session/workspace stay None.
    http_request.state.request_id = (
        validated_id_header(http_request.headers.get("X-Request-ID"))
        or uuid4().hex
    )
    http_request.state.session_id = validated_id_header(
        http_request.headers.get("X-Session-ID")
    )
    http_request.state.workspace_id = validated_id_header(
        http_request.headers.get("X-Workspace-ID")
    )
    _run_identity = resolve_run_identity(
        http_request,
        request_id=http_request.state.request_id,
        workspace_id=http_request.state.workspace_id,
    )
    http_request.state.run_id = _run_identity["run_id"]
    http_request.state.attempt_id = _run_identity["attempt_id"]
    http_request.state.client_request_id = _run_identity["client_request_id"]


def _build_run_context(
    http_request: Request,
    *,
    staff_code: str | None = None,
) -> RunContext:
    state: Any = getattr(http_request, "state", None)
    request_id = getattr(state, "request_id", None)
    run_id = coerce_uuid(getattr(state, "run_id", None)) or coerce_uuid(
        request_id
    ) or uuid4()
    workspace_id = coerce_uuid(
        getattr(state, "workspace_id", None),
        namespace="workspace",
    )
    return RunContext(
        run_id=run_id,
        workspace_id=workspace_id,
        attempt=parse_attempt(getattr(state, "attempt_id", None)) or 1,
        request_id=request_id,
        session_id=getattr(state, "session_id", None),
        staff_code=staff_code,
    )


@contextmanager
def request_run_context(
    http_request: Request,
    *,
    staff_code: str | None = None,
) -> Iterator[RunContext]:
    """Install a RunContext for the request."""
    run_context = _build_run_context(http_request, staff_code=staff_code)
    with set_run_context(
        run_id=run_context.run_id,
        workspace_id=run_context.workspace_id,
        attempt=run_context.attempt,
        request_id=run_context.request_id,
        session_id=run_context.session_id,
        staff_code=run_context.staff_code,
    ):
        yield run_context


def build_service_run_context(
    *,
    run_id: UUID,
    attempt: int,
    workspace_id: UUID | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    staff_code: str | None = None,
) -> RunContext:
    """Freeze a RunContext for an internal service boundary.

    Unlike :func:`request_run_context`, the durable identity comes from the
    validated path parameters (never minted from caller-chosen headers).
    """
    return RunContext(
        run_id=run_id,
        workspace_id=workspace_id,
        attempt=attempt,
        request_id=request_id,
        session_id=session_id,
        staff_code=staff_code,
    )


def format_sse_event(event: Any) -> str:
    """Render one legacy SSE frame (event/data) exactly as before."""
    payload = (
        event.data.model_dump() if isinstance(event.data, BaseModel) else event.data
    )
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event.event}\ndata: {data}\n\n"


def project_error_response(
    status: int,
    *,
    detail: str,
    error_code: str | None = None,
) -> JSONResponse:
    """Project an error JSON envelope: ``{detail}`` or ``{detail, error_code}``."""
    content: dict[str, str] = {"detail": detail}
    if error_code is not None:
        content["error_code"] = error_code
    return JSONResponse(status_code=status, content=content)
