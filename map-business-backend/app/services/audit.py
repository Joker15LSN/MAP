"""Admin write gate (R1-AUDIT-01, simplified in R2-P1-02).

``admin_write_guard`` is the authorization dependency attached to every
admin write route: it enforces the platform_admin role via the unified
:class:`PermissionService` and yields the trusted RequestPrincipal.

Auditing is NOT done here anymore: every state-changing write goes
through :class:`ConfigMutationService.apply_mutation`, which produces
exactly one hash-chained ``config_audit_events`` row per
applied/failed/rejected attempt and never swallows an audit failure.
The legacy ``audit_logs`` table keeps old history only; no new product
write lands there.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends

from ..api.deps import require_admin
from ..core.identity import RequestPrincipal


async def admin_write_guard(
    principal: RequestPrincipal = Depends(require_admin),
) -> AsyncIterator[RequestPrincipal]:
    """Authorization-only gate: 403 without platform_admin."""
    yield principal
