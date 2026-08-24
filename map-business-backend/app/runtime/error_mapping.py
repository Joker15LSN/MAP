"""Typed-error projection (P0-CONTRACT-01, run.md section 7, review R-09).

Single source of truth for HTTP status codes and SSE error frames of the
canonical runtime errors. Future /api/v1 routes and the SSE writer must use
these helpers instead of hand-written status logic; the contract test
verifies this table directly (no test-internal copy of the codes).
"""

from __future__ import annotations

import json
from typing import Final

from .event_envelope import (
    ARTIFACT_PAYLOAD_TOO_LARGE,
    ARTIFACT_REF_INVALID,
    EVENT_ENVELOPE_INVALID,
    EVENT_STALE_SEQ,
    IDEMPOTENCY_CONFLICT,
    PAYLOAD_NOT_SERIALIZABLE,
    UNKNOWN_EVENT_TYPE,
    UNKNOWN_EVENT_VERSION,
)
from .state_machine import RUN_TERMINAL_STATE, STATE_TRANSITION_VIOLATION

# CAPABILITY_DISABLED lives in map_core (tool_executor); the BFF projection
# keeps the same code constant so HTTP/SSE semantics stay aligned.
CAPABILITY_DISABLED: Final[str] = "CAPABILITY_DISABLED"

HTTP_STATUS_BY_ERROR_CODE: Final[dict[str, int]] = {
    STATE_TRANSITION_VIOLATION: 409,
    IDEMPOTENCY_CONFLICT: 409,
    RUN_TERMINAL_STATE: 409,
    EVENT_STALE_SEQ: 409,
    CAPABILITY_DISABLED: 409,
    UNKNOWN_EVENT_VERSION: 400,
    UNKNOWN_EVENT_TYPE: 400,
    PAYLOAD_NOT_SERIALIZABLE: 400,
    ARTIFACT_REF_INVALID: 400,
    EVENT_ENVELOPE_INVALID: 400,
    ARTIFACT_PAYLOAD_TOO_LARGE: 413,
}

DEFAULT_HTTP_STATUS = 500


def http_status_for(code: str) -> int:
    """HTTP status for a typed error code; unknown codes fail closed as 500."""
    return HTTP_STATUS_BY_ERROR_CODE.get(code, DEFAULT_HTTP_STATUS)


def sse_error_frame(code: str, message: str) -> str:
    """SSE error event frame carrying the typed code (client-parseable)."""
    payload = json.dumps({"code": code, "message": message}, ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n"
