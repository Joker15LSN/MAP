"""Application factory and router registration.

F-01 goal: ``main.py`` only owns the app factory, middleware, router
registration and lifespan. Routes live in ``app/api/*``; payload builders
in ``app/services/runtime_payloads.py``; the config repository protocol in
``app/repositories/config.py``.

``create_app`` accepts overrides so tests can inject a fake store / core
client instead of relying on module-level singletons. The module-level
``app`` / ``store`` / ``core_client`` names are kept as compatibility
re-exports for existing tests and uvicorn entry points.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api.admin_assets import router as admin_assets_router
from .api.admin_config import router as admin_config_router
from .api.admin_master import router as admin_master_router
from .api.chat import router as chat_router
from .api.conversations import router as conversations_router
from .api.feedback import router as feedback_router
from .core.identity import AuthMode, parse_optional_id, parse_request_id
from .core_client import MapCoreClient
from .repositories.config import ConfigRepository
from .settings import Settings, load_settings
from .store import AdminStateStore
from .telemetry import configure_bff_telemetry, shutdown_bff_telemetry


def create_app(
    *,
    settings: Settings | None = None,
    store: ConfigRepository | None = None,
    core_client: MapCoreClient | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: process settings; defaults to the environment.
        store: config repository; defaults to the file-backed
            :class:`AdminStateStore` pointed at ``settings.state_file``.
        core_client: map_core HTTP client; defaults to the client pointed
            at ``settings.map_core_api_origin``.
    """
    settings = settings or load_settings()
    store = store or AdminStateStore(settings.state_file)
    core_client = core_client or MapCoreClient(settings.map_core_api_origin)

    if settings.auth_mode == AuthMode.DEV and _looks_like_production():
        raise RuntimeError(
            "MAP_AUTH_MODE=dev is forbidden in production; "
            "set MAP_AUTH_MODE=trusted_header and MAP_TRUSTED_PROXY_REQUIRED=true"
        )

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Telemetry is configured once at import time above (idempotent); the
        # lifespan only owns the process-exit shutdown. OTel's global
        # TracerProvider and the FastAPI/httpx instrumentation cannot be
        # reinstalled in-process, so shutdown is a terminal state.
        yield
        shutdown_bff_telemetry()

    app = FastAPI(
        title="MAP Business Backend",
        description="MAP business management BFF. Frontend talks to this service only.",
        version="0.2.0",
        lifespan=_lifespan,
    )

    # SERVER/CLIENT spans + dynamic traceparent injection (no-op unless
    # MAP_OTEL_ENABLED is truthy). Must run before requests are served.
    configure_bff_telemetry(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.store = store
    app.state.core_client = core_client

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        # F-04: request/session/workspace ID ownership lives in the BFF.
        # Accept a legal inbound request id, else mint one; echo it back so
        # the browser can correlate errors. Session ids are stable per
        # browser session (persisted by the frontend); the BFF only mints
        # one when the client did not provide one.
        request.state.request_id = parse_request_id(request.headers.get("X-Request-ID"))
        request.state.session_id = parse_optional_id(request.headers.get("X-Session-ID"))
        request.state.workspace_id = parse_optional_id(
            request.headers.get("X-Workspace-ID")
        ) or settings.default_workspace_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    # Global identity gate (F-04): OIDC is not implemented before R3 and must
    # fail closed; trusted_header mode requires the proxy secret / X-UserId on
    # every request, not only on admin writes.
    @app.middleware("http")
    async def identity_gate(request: Request, call_next):
        if settings.auth_mode == AuthMode.OIDC:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=501,
                content={"code": "NOT_IMPLEMENTED", "message": "auth_mode=oidc not implemented yet"},
            )
        if settings.auth_mode == AuthMode.TRUSTED_HEADER:
            if settings.trusted_proxy_required:
                secret = request.headers.get("X-Trusted-Proxy-Secret", "")
                if not settings.trusted_proxy_secret or secret != settings.trusted_proxy_secret:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(status_code=401, content={"code": "AUTHENTICATION_REQUIRED"})
            if not (request.headers.get("X-UserId") or "").strip():
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=401, content={"code": "AUTHENTICATION_REQUIRED"})
        return await call_next(request)

    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(feedback_router)
    app.include_router(admin_config_router)
    app.include_router(admin_master_router)
    app.include_router(admin_assets_router)

    return app


def _looks_like_production() -> bool:
    env = os.getenv("MAP_ENV", "").strip().lower()
    return env in {"prod", "production"}


# Compatibility module-level singletons: uvicorn `app.main:app` and the
# existing tests import these names directly. New code should prefer
# ``create_app(overrides)``.
app = create_app()
store = app.state.store
core_client = app.state.core_client
