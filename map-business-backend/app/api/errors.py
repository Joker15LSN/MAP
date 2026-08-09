"""Standard error envelope for /api/v1 (FIX-P0-AUTH-01, FIX-P2-CONTRACT-E2E-01).

Every new /api/v1 error returns ``{code, message, details, request_id}``.
Legacy endpoints (``/api/chat*``, ``/api/admin/*``) keep FastAPI's default
``{"detail": ...}`` shape so old clients are unaffected.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

DEFAULT_CODES = {
    400: "BAD_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    409: "VERSION_CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


def error_envelope(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: object = None,
) -> dict:
    return {
        "code": code,
        "message": message,
        "details": details,
        "request_id": getattr(request.state, "request_id", None),
    }


def http_exception_response(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Build the standard /api/v1 envelope response for an HTTPException."""
    code = exc.headers.get("X-MAP-Error-Code") if exc.headers else None
    if not code:
        code = DEFAULT_CODES.get(exc.status_code, "ERROR")
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(request, exc.status_code, code, message),
        headers={k: v for k, v in (exc.headers or {}).items() if k != "X-MAP-Error-Code"},
    )


def _is_new_api(path: str) -> bool:
    return path.startswith(("/api/v1", "/internal/v1"))


def install_error_handlers(app: FastAPI) -> None:
    """Register envelope handlers for /api/v1 errors (legacy paths untouched)."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if not _is_new_api(request.url.path):
            # Legacy contract: keep the default {"detail": ...} body.
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return http_exception_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if not _is_new_api(request.url.path):
            return JSONResponse(status_code=422, content={"detail": exc.errors()})
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                request,
                422,
                "VALIDATION_ERROR",
                "request validation failed",
                details=exc.errors(),
            ),
        )
