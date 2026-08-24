"""S4-01: freeze the durable run identity at the request boundary.

The six-field durable identity chain (workspace / run / step / attempt /
invocation / client_request) must be frozen at the request boundary and
carried through AgentRequest.extra into the sandbox tool. workspace_id
already arrives via X-Workspace-ID; this module freezes the remaining
request-level fields (run_id, attempt_id, client_request_id), honoring
valid inbound headers and otherwise minting deterministic values.
step_id and invocation_id are per-tool-call and are injected by
ToolExecutor (step_index / tool_call_id).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from fastapi import Request

# Shared ID contract (mirrored by the BFF and map_core): non-empty, at most
# 128 chars, charset [A-Za-z0-9._:-].
_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

RUN_HEADER = "X-Run-ID"
ATTEMPT_HEADER = "X-Attempt-ID"
CLIENT_REQUEST_HEADER = "X-Client-Request-ID"


def _valid_id(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value and _ID_RE.fullmatch(value):
        return value
    return None


def _run_id_from_request_id(request_id: str | None) -> str | None:
    if isinstance(request_id, str) and request_id and request_id != "missing":
        return request_id
    return None


def resolve_run_identity(
    http_request: Request | None,
    *,
    request_id: str | None,
    workspace_id: str | None,
) -> dict[str, str | None]:
    """Freeze run_id / attempt_id / client_request_id for one request.

    - run_id: honor X-Run-ID, else reuse request_id, else mint uuid4().hex;
    - attempt_id: honor X-Attempt-ID, else att-1;
    - client_request_id: honor X-Client-Request-ID, else run_id.
    """
    headers: Any = http_request.headers if http_request is not None else {}
    run_id = (
        _valid_id(headers.get(RUN_HEADER))
        or _run_id_from_request_id(request_id)
        or uuid4().hex
    )
    attempt_id = _valid_id(headers.get(ATTEMPT_HEADER)) or "att-1"
    client_request_id = _valid_id(headers.get(CLIENT_REQUEST_HEADER)) or run_id
    return {
        "workspace_id": workspace_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "client_request_id": client_request_id,
    }
