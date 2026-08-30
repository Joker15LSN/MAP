"""Backward-compatible RunContext shim.

The canonical implementation lives in
:mod:`map_core.routers.runtime_transport`.  This module is retained only for
L3 compatibility and is deleted in the next commit.
"""

from __future__ import annotations

from fastapi import Request

from ..service.execution_event import RunContext
from .runtime_transport import (
    _build_run_context,
)
from .runtime_transport import (
    build_service_run_context as build_service_run_context,
)
from .runtime_transport import (
    request_run_context as request_run_context,
)


def build_run_context(
    http_request: Request,
    *,
    staff_code: str | None = None,
) -> RunContext:
    return _build_run_context(http_request, staff_code=staff_code)
