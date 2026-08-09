"""Readiness endpoint (FIX-P1-DEPLOY-01).

``/ready`` proves the product database is actually usable: reachable,
migrated to head, and carrying the default workspace seed. ``/health``
(liveness) stays downstream-free and only reports process state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db.session import get_engine
from ..settings import DEFAULT_WORKSPACE_CODE

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"


def current_head_revision() -> str | None:
    """Return the head revision id from the local migration scripts."""
    try:
        scripts = ScriptDirectory(str(MIGRATIONS_DIR))
        return scripts.get_current_head()
    except Exception:  # noqa: BLE001 - readiness must degrade to "not ready"
        logger.exception("failed to resolve alembic head revision")
        return None


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Return 200 only when DB is reachable, migrated to head and seeded."""
    checks: dict[str, object] = {}
    ok = True

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["database"] = {"ok": False, "error": str(exc)[:200]}

    if ok:
        head = current_head_revision()
        try:
            async with engine.connect() as conn:
                current = (
                    await conn.execute(
                        text("SELECT version_num FROM map_control.alembic_version")
                    )
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
            checks["migration"] = {"ok": False, "error": str(exc)[:200]}

    if ok:
        try:
            async with engine.connect() as conn:
                seeded = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM map_control.workspaces "
                            "WHERE code = :code LIMIT 1"
                        ),
                        {"code": DEFAULT_WORKSPACE_CODE},
                    )
                ).scalar_one_or_none()
            checks["seed"] = {"default_workspace": bool(seeded), "ok": bool(seeded)}
            if not seeded:
                ok = False
        except Exception as exc:  # noqa: BLE001
            ok = False
            checks["seed"] = {"ok": False, "error": str(exc)[:200]}

    status_code = 200 if ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
