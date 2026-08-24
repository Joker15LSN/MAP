"""Readiness endpoint (FIX-P1-DEPLOY-01, review R-06).

/ready proves the product database is actually usable: configured,
reachable, migrated to head, and carrying the default workspace seed.
Missing/malformed/unreachable DSN, unmigrated schema or a missing seed all
return HTTP 503 with a fixed body shape - never a bare 200. /health
(liveness, see app/api/chat.py) stays downstream-free.

Response bodies must never contain connection credentials: every error
detail is passed through redact_dsn before serialization.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ..settings import DEFAULT_WORKSPACE_CODE, DEFAULT_WORKSPACE_ID

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"

# asyncpg connect timeout keeps /ready from hanging on blackholed networks.
DB_CONNECT_TIMEOUT_SECONDS = 5

_DSN_CRED_PATTERN = re.compile(r"(://[^/\s:@]+):([^/\s@]+)@")
_DSN_PARAM_PATTERN = re.compile(r"([?&](?:password|passwd|pwd)=)[^&\s]+", re.IGNORECASE)


def redact_dsn(text: str) -> str:
    """Remove userinfo passwords and password query params from a string."""
    redacted = _DSN_CRED_PATTERN.sub(r"\1:<redacted>@", text)
    return _DSN_PARAM_PATTERN.sub(r"\1<redacted>", redacted)


def current_head_revision() -> str | None:
    """Return the head revision id from the local migration scripts."""
    try:
        scripts = ScriptDirectory(str(MIGRATIONS_DIR))
        return scripts.get_current_head()
    except Exception:  # readiness must degrade to "not ready"
        logger.exception("failed to resolve alembic head revision")
        return None


def _check_error(label: str, exc: Exception) -> dict[str, object]:
    """Build a check failure body; the message is redacted and DSN-free."""
    message = redact_dsn(str(exc))[:200]
    return {"ok": False, "error": message}


def _not_ready(checks: dict[str, object]) -> JSONResponse:
    return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})


@router.get(
    "/ready",
    responses={
        200: {"description": "All required dependencies are available"},
        503: {"description": "Not ready: DSN missing/malformed/unreachable, "
                             "schema behind head, or seed missing"},
    },
)
async def ready(request: Request) -> JSONResponse:
    """Return 200 only when DB is configured, reachable, migrated and seeded.

    Uses its own short-lived connection (NullPool) so readiness never
    depends on or pollutes the shared engine pool. R-06: a missing or empty
    DSN is HTTP 503 with the fixed body shape (never FastAPI's default 200).
    """
    checks: dict[str, object] = {}
    ok = True

    dsn = os.getenv("MAP_CONTROL_DB_DSN", "").strip()
    if not dsn:
        # P0-SEC-01 / R-06: no repository default DSN - degrade to "not
        # ready" with the fixed 503 envelope (no DSN material involved).
        checks["database"] = {
            "ok": False,
            "error": "MAP_CONTROL_DB_DSN is not configured",
        }
        return _not_ready(checks)

    try:
        engine = create_async_engine(
            dsn,
            poolclass=NullPool,
            pool_pre_ping=True,
            connect_args={"timeout": DB_CONNECT_TIMEOUT_SECONDS},
        )
    except Exception as exc:  # noqa: BLE001 - malformed DSN
        logger.warning("MAP_CONTROL_DB_DSN is malformed")
        checks["database"] = _check_error("database", exc)
        return _not_ready(checks)

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["database"] = _check_error("database", exc)

    if ok:
        head = current_head_revision()
        try:
            async with engine.connect() as conn:
                current = (
                    await conn.execute(text("SELECT version_num FROM map_control.alembic_version"))
                ).scalar_one_or_none()
            checks["migration"] = {
                "current": current,
                "head": head,
                "ok": head is not None and current == head,
            }
            if not checks["migration"]["ok"]:
                ok = False
        except Exception as exc:  # noqa: BLE001
            ok = False
            checks["migration"] = _check_error("migration", exc)

    if ok:
        # R2-P2-04: the seed is only valid if the STABLE UUID *and* the
        # business code both match; a row with the right code but a wrong
        # id must fail readiness (503).
        expected_id = os.getenv("MAP_DEFAULT_WORKSPACE_ID", DEFAULT_WORKSPACE_ID)
        try:
            async with engine.connect() as conn:
                seeded = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM map_control.workspaces "
                            "WHERE id = :id AND code = :code LIMIT 1"
                        ),
                        {"id": expected_id, "code": DEFAULT_WORKSPACE_CODE},
                    )
                ).scalar_one_or_none()
            checks["seed"] = {
                "default_workspace_id": expected_id,
                "default_workspace_code": DEFAULT_WORKSPACE_CODE,
                "ok": bool(seeded),
            }
            if not seeded:
                ok = False
        except Exception as exc:  # noqa: BLE001
            ok = False
            checks["seed"] = _check_error("seed", exc)

    await engine.dispose()
    status_code = 200 if ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
