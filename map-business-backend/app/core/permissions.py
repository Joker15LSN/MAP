"""Permission decisions (FIX-P0-AUTH-01).

All admin authorization funnels through :class:`PermissionService` so no
router hard-codes role strings. Admin writes require ``platform_admin``
(or an equivalent workspace scope in R3); read-only audit viewers get
their own gate.
"""

from __future__ import annotations

from ..core.identity import RequestPrincipal

PLATFORM_ADMIN = "platform_admin"
AUDIT_VIEWER = "audit_viewer"


class PermissionDenied(Exception):
    """Raised when a principal lacks the required permission."""


class PermissionService:
    """Single place for role/scope decisions."""

    def has_role(self, principal: RequestPrincipal, role: str) -> bool:
        return role in principal.roles

    def require_admin(self, principal: RequestPrincipal) -> RequestPrincipal:
        """Gate admin writes: platform_admin (or workspace scope in R3)."""
        if not self.has_role(principal, PLATFORM_ADMIN):
            raise PermissionDenied(PLATFORM_ADMIN)
        return principal

    def require_audit_viewer(self, principal: RequestPrincipal) -> RequestPrincipal:
        """Gate audit reads: platform_admin or audit_viewer."""
        if not (
            self.has_role(principal, PLATFORM_ADMIN)
            or self.has_role(principal, AUDIT_VIEWER)
        ):
            raise PermissionDenied(AUDIT_VIEWER)
        return principal


def get_permission_service() -> PermissionService:
    return PermissionService()
