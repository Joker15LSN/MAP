"""Shared FastAPI dependencies.

F-01: routers receive ``store`` / ``core_client`` through ``app.state`` so
that ``create_app(store=..., core_client=...)`` overrides work for tests,
instead of closing over module-level singletons.

F-04 / FIX-P0-AUTH-01: ``get_principal`` resolves the trusted
:class:`RequestPrincipal` once per request (cached on ``request.state``);
business services depend on it instead of reading arbitrary headers.
``require_admin`` funnels through :class:`PermissionService`.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ..core.identity import (
    AuthMode,
    RequestPrincipal,
    parse_optional_id,
)
from ..core.permissions import PermissionDenied, PermissionService
from ..core.service_identity import constant_time_equal
from ..core_client import MapCoreClient
from ..repositories.config import ConfigRepository
from ..settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> ConfigRepository:
    return request.app.state.store


def get_core_client(request: Request) -> MapCoreClient:
    return request.app.state.core_client


def get_permissions(request: Request) -> PermissionService:
    return request.app.state.permissions


def get_run_application(request: Request):
    """Canonical Run application wired by ``create_app`` (lazy if absent)."""
    application = getattr(request.app.state, "run_application", None)
    if application is None:
        from ..db.session import get_session_factory
        from ..runs import PgRunStore, RunApplication

        application = RunApplication(PgRunStore(get_session_factory()))
        request.app.state.run_application = application
    return application


def get_turn_application(request: Request):
    """Turn application (Step 4) wired lazily over the Run application."""
    application = getattr(request.app.state, "turn_application", None)
    if application is None:
        from ..db.session import get_session_factory
        from ..turns import TurnApplication

        application = TurnApplication(
            get_session_factory(), get_run_application(request)
        )
        request.app.state.turn_application = application
    return application


def _parse_roles(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _unauthorized(
    code: str = "AUTHENTICATION_REQUIRED", message: str | None = None
) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=message or "authentication required",
        headers={"X-MAP-Error-Code": code},
    )


def resolve_principal(request: Request) -> RequestPrincipal:
    """Resolve the trusted principal for the current request.

    - ``dev``: fixed local administrator (non-prod only; enforced at startup).
    - ``trusted_header``: read identity from headers, requiring the shared
      proxy secret (constant-time compare). The verification is mandatory:
      startup fails when the mode is enabled without a secret.
    - ``oidc``: not implemented in R1/R2 (R3-ID); fail closed with 501.
    """
    settings: Settings = request.app.state.settings
    if settings.auth_mode == AuthMode.DEV:
        return RequestPrincipal(
            subject="local-admin",
            user_id="local-admin",
            staff_code=None,
            display_name="本地管理员",
            roles=("platform_admin",),
            department_code=None,
            workspace_id=settings.default_workspace_id,
            auth_mode=AuthMode.DEV,
        )

    if settings.auth_mode == AuthMode.TRUSTED_HEADER:
        if settings.trusted_proxy_required:
            secret = request.headers.get("X-Trusted-Proxy-Secret", "")
            if not settings.trusted_proxy_secret or not constant_time_equal(
                secret, settings.trusted_proxy_secret
            ):
                raise _unauthorized(message="untrusted proxy identity") from None
        subject = (request.headers.get("X-UserId") or "").strip()
        if not subject:
            raise _unauthorized(message="missing X-UserId") from None
        workspace = parse_optional_id(request.headers.get("X-Workspace-ID")) or (
            settings.default_workspace_id
        )
        return RequestPrincipal(
            subject=subject,
            user_id=subject,
            staff_code=request.headers.get("X-User-Staff-Code") or None,
            display_name=(request.headers.get("X-UserName") or subject).strip() or subject,
            roles=_parse_roles(request.headers.get("X-User-Roles")),
            department_code=request.headers.get("X-User-Department") or None,
            workspace_id=workspace,
            auth_mode=AuthMode.TRUSTED_HEADER,
        )

    # OIDC lands in R3-ID; never silently downgrade to anonymous.
    raise HTTPException(status_code=501, detail="auth_mode=oidc not implemented yet")


def get_principal(request: Request) -> RequestPrincipal:
    if not hasattr(request.state, "principal"):
        request.state.principal = resolve_principal(request)
    return request.state.principal


def require_admin(
    principal: RequestPrincipal = Depends(get_principal),
    permissions: PermissionService = Depends(get_permissions),
) -> RequestPrincipal:
    """Admin write gate via the unified permission service."""
    try:
        return permissions.require_admin(principal)
    except PermissionDenied:
        raise HTTPException(
            status_code=403,
            detail="platform_admin role required",
            headers={"X-MAP-Error-Code": "FORBIDDEN"},
        ) from None


def require_audit_viewer(
    principal: RequestPrincipal = Depends(get_principal),
    permissions: PermissionService = Depends(get_permissions),
) -> RequestPrincipal:
    """Read-only audit access gate (platform_admin or audit_viewer)."""
    try:
        return permissions.require_audit_viewer(principal)
    except PermissionDenied:
        raise HTTPException(
            status_code=403,
            detail="audit_viewer role required",
            headers={"X-MAP-Error-Code": "FORBIDDEN"},
        ) from None
