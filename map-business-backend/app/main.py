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

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.admin_assets import router as admin_assets_router
from .api.admin_config import router as admin_config_router
from .api.admin_master import router as admin_master_router
from .api.chat import router as chat_router
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

    app.state.store = store
    app.state.core_client = core_client

    app.include_router(chat_router)
    app.include_router(admin_config_router)
    app.include_router(admin_master_router)
    app.include_router(admin_assets_router)

    return app


# Compatibility module-level singletons: uvicorn `app.main:app` and the
# existing tests import these names directly. New code should prefer
# ``create_app(overrides)``.
app = create_app()
store = app.state.store
core_client = app.state.core_client
