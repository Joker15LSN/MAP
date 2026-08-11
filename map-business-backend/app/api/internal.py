"""Internal service-to-service API (FIX-P0-AUTH-01, hardened in R2-P0-02).

Only authenticated service principals may call these endpoints; the route
is split out of the user-principal middleware in ``app.main`` so a valid
service credential alone is sufficient (and required). Authorization is
decided exclusively by the matched credential's inherent claims
(token reference -> metadata); ``X-Service-*`` headers never grant
anything. Browser/user tokens are rejected.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..core.identity import ServicePrincipal
from ..core.service_identity import (
    ServiceAuthenticationError,
    authenticate_service,
)
from ..settings import Settings
from .deps import get_settings

router = APIRouter(prefix="/internal/v1", tags=["internal"])


def require_service(*scopes: str):
    """Dependency factory: authenticate a service principal and require scopes."""

    def _dep(request: Request, settings: Settings = Depends(get_settings)) -> ServicePrincipal:
        try:
            principal = authenticate_service(request, credentials=settings.service_credentials)
        except ServiceAuthenticationError as exc:
            code = exc.args[1] if len(exc.args) > 1 else "INVALID_SERVICE_IDENTITY"
            if code == "FORBIDDEN":
                raise HTTPException(
                    status_code=403,
                    detail="service identity forbidden",
                    headers={"X-MAP-Error-Code": "FORBIDDEN"},
                ) from None
            raise HTTPException(
                status_code=401,
                detail="invalid service identity",
                headers={"X-MAP-Error-Code": "INVALID_SERVICE_IDENTITY"},
            ) from None
        # Audience must match the BFF's configured audience.
        if settings.service_audience and principal.audience != settings.service_audience:
            raise HTTPException(
                status_code=401,
                detail="service audience mismatch",
                headers={"X-MAP-Error-Code": "INVALID_SERVICE_IDENTITY"},
            )
        if scopes and not set(scopes).issubset(set(principal.scopes)):
            raise HTTPException(
                status_code=403,
                detail=f"service scope {scopes} required",
                headers={"X-MAP-Error-Code": "FORBIDDEN"},
            )
        return principal

    return _dep


@router.get("/ping")
async def ping(
    principal: ServicePrincipal = Depends(require_service("internal.ping")),
) -> dict:
    return {
        "service": principal.service_name,
        "audience": principal.audience,
        "scopes": list(principal.scopes),
        "key_id": principal.key_id,
    }
