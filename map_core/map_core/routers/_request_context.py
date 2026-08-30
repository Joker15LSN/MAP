"""Request-boundary RunContext wiring shared by the chat routers.

The chat routers install a ``RunContext`` around the request-handling
block (route body for non-stream endpoints, stream-iteration body for SSE
endpoints).  Sandbox routes are intentionally not touched.

Internal service routes (execution_router) use
:func:`build_service_run_context` instead: they freeze run_id/attempt from
the validated path parameters.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4

from fastapi import Request

from ..service.execution_event import (
    RunContext,
    coerce_uuid,
    set_run_context,
)

_ATTEMPT_RE = re.compile(r"^att-(\d+)$")


def _parse_attempt(raw: Any) -> int:
    if isinstance(raw, int) and raw >= 1:
        return raw
    if isinstance(raw, str):
        match = _ATTEMPT_RE.fullmatch(raw.strip())
        if match:
            return int(match.group(1))
    return 1


def build_run_context(
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
        attempt=_parse_attempt(getattr(state, "attempt_id", None)),
        request_id=request_id,
        session_id=getattr(state, "session_id", None),
        staff_code=staff_code,
    )


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

    Unlike :func:`build_run_context`, the durable identity comes from the
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


@contextmanager
def request_run_context(
    http_request: Request,
    *,
    staff_code: str | None = None,
) -> Iterator[RunContext]:
    """Install a RunContext for the request."""
    run_context = build_run_context(http_request, staff_code=staff_code)
    with set_run_context(
        run_id=run_context.run_id,
        workspace_id=run_context.workspace_id,
        attempt=run_context.attempt,
        request_id=run_context.request_id,
        session_id=run_context.session_id,
        staff_code=run_context.staff_code,
    ):
        yield run_context
