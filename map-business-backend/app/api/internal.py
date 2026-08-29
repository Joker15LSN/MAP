"""Internal service-to-service API (FIX-P0-AUTH-01, hardened in R2-P0-02).

Only authenticated service principals may call these endpoints; the route
is split out of the user-principal middleware in ``app.main`` so a valid
service credential alone is sufficient (and required). Authorization is
decided exclusively by the matched credential's inherent claims
(token reference -> metadata); ``X-Service-*`` headers never grant
anything. Browser/user tokens are rejected.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..core.identity import ServicePrincipal
from ..core.service_identity import (
    ServiceAuthenticationError,
    authenticate_service,
)
from ..db.session import DbSession
from ..services.runtime_snapshot.adapters.pg import PgRuntimeSnapshotRepository
from ..services.runtime_snapshot.digest import projection_digest
from ..services.runtime_snapshot.schemas import RuntimeSnapshotRead
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


@router.get("/runtime-config-snapshots/{snapshot_id}")
async def get_runtime_config_snapshot(
    snapshot_id: uuid.UUID,
    response: Response,
    session: DbSession,
    _: ServicePrincipal = Depends(require_service("runtime-config.snapshots.read")),
) -> RuntimeSnapshotRead:
    """Read one immutable runtime snapshot by id (no current-pointer fetch).

    Draft snapshots are not readable (404, same as missing). The digest is
    recomputed from the stored projection on every read; any mismatch is a
    fail-closed 500, never a successful response.
    """
    record = await PgRuntimeSnapshotRepository(session).get(snapshot_id)
    if record is None or record.status == "draft":
        raise HTTPException(
            status_code=404,
            detail="runtime config snapshot not found",
            headers={"X-MAP-Error-Code": "SNAPSHOT_NOT_FOUND"},
        )

    recomputed_digest = projection_digest(record.projection)
    if recomputed_digest != record.digest:
        raise HTTPException(
            status_code=500,
            detail="runtime config snapshot digest mismatch",
            headers={"X-MAP-Error-Code": "SNAPSHOT_DIGEST_MISMATCH"},
        )

    response.headers["ETag"] = f'"{record.digest}"'
    response.headers["X-MAP-Snapshot-Digest"] = record.digest
    response.headers["Cache-Control"] = "no-store"
    return RuntimeSnapshotRead(
        id=record.id,
        schema_version=record.schema_version,
        digest=record.digest,
        parent_id=record.parent_id,
        created_at=record.created_at,
        projection=record.projection,
    )
