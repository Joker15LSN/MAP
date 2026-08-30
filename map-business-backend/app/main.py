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

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api.admin_assets import router as admin_assets_router
from .api.admin_config import router as admin_config_router
from .api.admin_master import router as admin_master_router
from .api.admin_snapshots import router as admin_snapshots_router
from .api.audit_events import router as audit_events_router
from .api.chat import router as chat_router
from .api.conversations import router as conversations_router
from .api.deps import get_principal
from .api.errors import install_error_handlers
from .api.feedback import router as feedback_router
from .api.internal import router as internal_router
from .api.readiness import router as readiness_router
from .api.runs import router as runs_router
from .core.identity import AuthMode, parse_optional_id, parse_request_id
from .core.permissions import PermissionService
from .core_client import MapCoreClient
from .cors_policy import is_production, normalize_env, parse_origins
from .services.stream_registry import StreamRegistry
from .settings import Settings, load_settings
from .telemetry import configure_bff_telemetry, shutdown_bff_telemetry

logger = logging.getLogger(__name__)


def validate_settings(settings: Settings) -> None:
    """Fail fast on unsafe identity/env/CORS configurations (fail-closed)."""
    # S3-04 / S4-06: strict environment schema - unknown MAP_ENV fails
    # closed instead of silently running as dev.
    env = normalize_env(settings.env)
    if settings.auth_mode == AuthMode.DEV and is_production(env):
        raise RuntimeError(
            "MAP_AUTH_MODE=dev is forbidden in production; "
            "set MAP_AUTH_MODE=trusted_header and MAP_TRUSTED_PROXY_REQUIRED=true"
        )
    if settings.auth_mode == AuthMode.TRUSTED_HEADER:
        if not settings.trusted_proxy_required:
            raise RuntimeError(
                "MAP_TRUSTED_PROXY_REQUIRED=true is mandatory when "
                "MAP_AUTH_MODE=trusted_header (fail-closed)"
            )
        if not settings.trusted_proxy_secret:
            raise RuntimeError(
                "MAP_TRUSTED_PROXY_SECRET is required when "
                "MAP_AUTH_MODE=trusted_header (fail-closed)"
            )
    # AC-SEC-11 / R-10 / S3-04 / S4-06: shared CORS schema (single source of
    # truth: packages/cors_policy/cors_policy.py, vendored as app/cors_policy.py).
    # Every origin must be '*' or a well-formed http(s)://host[:port] with a
    # real hostname and no userinfo/path/query/fragment - malformed entries
    # fail at startup in EVERY environment, and wildcard CORS combined with
    # credentials is refused in production (fail-closed at startup).
    origins = parse_origins(settings.cors_origins)
    if is_production(env) and "*" in origins and settings.cors_allow_credentials:
        raise RuntimeError(
            "wildcard CORS with credentials is forbidden in production; "
            "set MAP_CORS_ORIGINS to explicit origins or "
            "MAP_CORS_ALLOW_CREDENTIALS=false (fail-closed)"
        )


def create_app(
    *,
    settings: Settings | None = None,
    store: Any = None,
    core_client: MapCoreClient | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: process settings; defaults to the environment.
        store: optional read-path override for tests; stored on
            ``app.state.store``. Production leaves this ``None`` and
            ``app.api.deps.get_store`` builds a session-bound
            ``PgAdminStateRepository``.
        core_client: map_core HTTP client; defaults to the client pointed
            at ``settings.map_core_api_origin``.
    """
    settings = settings or load_settings()
    validate_settings(settings)
    core_client = core_client or MapCoreClient(settings.map_core_api_origin)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Best-effort boot reconciliation (never blocks startup):
        # 1. drain legacy pending mutation rows when an old file store still
        #    exists (J7a only; J7b removes this path);
        # 2. seed the PG admin state singleton + an active runtime snapshot
        #    when the database is empty. Existing data is never overwritten.
        try:
            from .db.session import get_session_factory

            factory = get_session_factory()
            state_file = Path(settings.state_file)
            if state_file.exists():
                from .services.config_mutation import (
                    reconcile_config_mutations,
                )
                from .services.runtime_snapshot.adapters.pg import (
                    PgRuntimeSnapshotRepository,
                )
                from .services.runtime_snapshot.service import (
                    reconcile_runtime_snapshot_mutations,
                )
                from .store import AdminStateStore

                legacy_store = AdminStateStore(settings.state_file)
                await reconcile_config_mutations(factory, legacy_store)
                await reconcile_runtime_snapshot_mutations(
                    factory,
                    legacy_store,
                    lambda session: PgRuntimeSnapshotRepository(session),
                )

            from .schemas import AdminState
            from .services.runtime_snapshot.adapters.admin_state_pg import (
                PgAdminStateRepository,
            )
            from .services.runtime_snapshot.adapters.pg import (
                PgRuntimeSnapshotRepository,
            )
            from .services.runtime_snapshot.digest import (
                projection_digest,
                snapshot_id_for_digest,
            )
            from .services.runtime_snapshot.schemas import (
                build_runtime_projection,
            )

            async with factory() as session:
                admin_repo = PgAdminStateRepository(session)
                await admin_repo.seed_if_empty(AdminState.default())
                snapshot_repo = PgRuntimeSnapshotRepository(session)
                if await snapshot_repo.get_current() is None:
                    state = await admin_repo.load()
                    projection = build_runtime_projection(state)
                    digest = projection_digest(projection)
                    snapshot_id = snapshot_id_for_digest(digest)
                    inserted = await snapshot_repo.insert(
                        snapshot_id, projection, digest, None, "draft"
                    )
                    if inserted.status == "draft":
                        await snapshot_repo.transition_status(
                            snapshot_id, "draft", "published"
                        )
                    await snapshot_repo.activate(snapshot_id, None)
                await session.commit()
        except Exception:
            logger.exception("runtime snapshot boot reconciliation failed")
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

    cors_origins = list(parse_origins(settings.cors_origins))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.store = store
    app.state.core_client = core_client
    app.state.permissions = PermissionService()
    app.state.stream_registry = StreamRegistry()

    install_error_handlers(app)

    # Identity gate (F-04 / FIX-P0-AUTH-01): resolve the trusted principal
    # once per request and cache it on request.state; any later service only
    # reads the cached principal. dev mode uses the fixed local admin;
    # trusted_header requires the proxy secret (constant-time compare);
    # oidc fails closed with 501. Registered BEFORE request_context so the
    # request/session/workspace IDs are populated when the 401/501 envelope
    # is built.
    #
    # R2-P0-02: /internal/* is split out of the user-principal gate. These
    # routes accept ServicePrincipal only (require_service dependency,
    # fail-closed per route); a service credential must never need forged
    # user headers to enter, and user credentials alone can never satisfy
    # the service registry lookup.
    #
    # R3-P1-02: /health and /ready are infrastructure probes (docker
    # healthchecks cannot carry identity credentials). /health returns a
    # fixed liveness payload; /ready discloses only DB/migration/seed
    # status and is itself protected by the workspace UUID+code double
    # check (R2-P1-06) — no principal data is exposed either way.
    @app.middleware("http")
    async def identity_gate(request: Request, call_next):
        if request.url.path.startswith("/internal/") or request.url.path in (
            "/health",
            "/ready",
        ):
            return await call_next(request)
        if settings.auth_mode != AuthMode.DEV:
            from fastapi import HTTPException

            from .api.errors import http_exception_response

            try:
                get_principal(request)
            except HTTPException as exc:
                return http_exception_response(request, exc)
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        # F-04: request/session/workspace ID ownership lives in the BFF.
        # Accept a legal inbound request id, else mint one; echo it back so
        # the browser can correlate errors. Session ids are stable per
        # browser session (persisted by the frontend); the BFF only mints
        # one when the client did not provide one.
        request.state.request_id = parse_request_id(request.headers.get("X-Request-ID"))
        request.state.session_id = parse_optional_id(request.headers.get("X-Session-ID"))
        request.state.workspace_id = (
            parse_optional_id(request.headers.get("X-Workspace-ID"))
            or settings.default_workspace_id
        )
        # S4-01: freeze the durable run identity the map_core sandbox tool
        # needs (run/attempt/client_request). step/invocation are minted per
        # tool call inside map_core.
        request.state.run_id = (
            parse_optional_id(request.headers.get("X-Run-ID"))
            or request.state.request_id
        )
        request.state.attempt_id = (
            parse_optional_id(request.headers.get("X-Attempt-ID")) or "att-1"
        )
        request.state.client_request_id = (
            parse_optional_id(request.headers.get("X-Client-Request-ID"))
            or request.state.run_id
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    app.include_router(chat_router)
    app.include_router(readiness_router)
    app.include_router(audit_events_router)
    app.include_router(conversations_router)
    app.include_router(runs_router)
    app.include_router(internal_router)
    app.include_router(feedback_router)
    app.include_router(admin_config_router)
    app.include_router(admin_master_router)
    app.include_router(admin_assets_router)
    app.include_router(admin_snapshots_router)

    return app



# Compatibility module-level singletons: uvicorn `app.main:app` and the
# existing tests import these names directly. New code should prefer
# ``create_app(overrides)``.
#
# R2-P2-04: these are LAZY (PEP 562). Importing ``app.main`` must never
# touch the filesystem — eager construction here forced every importer to
# pre-set MAP_BFF_STATE_FILE to avoid creating /app/data, which is exactly
# the "pretending to be defaults" anti-pattern the second-round review
# rejected.
_lazy_app: FastAPI | None = None


def _compat_app() -> FastAPI:
    global _lazy_app
    if _lazy_app is None:
        _lazy_app = create_app()
    return _lazy_app


def __getattr__(name: str):
    if name == "app":
        return _compat_app()
    if name == "store":
        return _compat_app().state.store
    if name == "core_client":
        return _compat_app().state.core_client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
