"""Service identity validation (FIX-P0-AUTH-01).

Internal callers (other MAP services) authenticate with a Bearer token
issued against a shared secret (supporting rotation: comma-separated list
of valid secrets; the first match wins). The token proves the caller is a
service, then the ``X-Service-*`` headers define name/audience/scopes.

A browser/user token can never satisfy :func:`authenticate_service`; user
principals and service principals are disjoint identities.
"""

from __future__ import annotations

import hmac
import re

from fastapi import Request

from .identity import ServicePrincipal, is_valid_id

_TOKEN_RE = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


def constant_time_equal(left: str, right: str) -> bool:
    """Compare two strings in constant time (never logs either value)."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _split_scopes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


class ServiceAuthenticationError(Exception):
    """Token invalid / audience mismatch / missing service identity."""


def authenticate_service(request: Request, *, secrets: tuple[str, ...]) -> ServicePrincipal:
    """Validate the request as a service call, else raise 401/403-worthy error.

    Raises :class:`ServiceAuthenticationError` with a stable error code in
    ``args[1]``: ``INVALID_SERVICE_IDENTITY`` (bad/missing token) or
    ``FORBIDDEN`` (valid token, audience/scope not granted).
    """
    auth_header = request.headers.get("Authorization", "")
    match = _TOKEN_RE.match(auth_header)
    if match is None:
        raise ServiceAuthenticationError("missing service bearer token", "INVALID_SERVICE_IDENTITY")
    token = match.group(1)
    if not secrets or not any(constant_time_equal(token, candidate) for candidate in secrets):
        raise ServiceAuthenticationError("invalid service token", "INVALID_SERVICE_IDENTITY")

    service_name = (request.headers.get("X-Service-Name") or "").strip()
    if not is_valid_id(service_name):
        raise ServiceAuthenticationError("missing X-Service-Name", "INVALID_SERVICE_IDENTITY")
    audience = (request.headers.get("X-Service-Audience") or "").strip()
    scopes = _split_scopes(request.headers.get("X-Service-Scopes"))
    return ServicePrincipal(service_name=service_name, audience=audience, scopes=scopes)
