"""Service identity validation (FIX-P0-AUTH-01, hardened in R2-P0-02).

Security model:

- Every service holds a credential registered server-side: a
  ``token reference -> metadata`` mapping (``ServiceCredential``) binding
  the bearer token to its *inherent* claims — service name, audience,
  scopes and a rotation ``key_id``. Configuration is the JSON array in
  ``MAP_SERVICE_CREDENTIALS``.
- Authorization is decided ONLY by the inherent claims of the matched
  credential. ``X-Service-*`` headers are transport/debug information:
  they can never grant anything, and a header contradicting the inherent
  claims (other name, other audience, scopes beyond the grant) fails the
  request instead of silently diverging.
- Rotation = add a new credential entry with a fresh ``key_id`` (dual-key
  window), then revoke or remove the old entry. There is no global
  shared secret that every service could use to self-assert any identity.
- A browser/user token can never satisfy :func:`authenticate_service`;
  user principals and service principals are disjoint identities.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class ServiceCredential:
    """Server-side credential entry: token bound to fixed claims.

    ``key_id`` is the rotation key reference; multiple entries for the
    same service form the dual-key rotation window. ``revoked=True``
    keeps the entry auditable while rejecting the token.
    """

    key_id: str
    token: str
    service_name: str
    audience: str
    scopes: tuple[str, ...] = field(default_factory=tuple)
    revoked: bool = False


class ServiceCredentialConfigError(ValueError):
    """Malformed ``MAP_SERVICE_CREDENTIALS`` (fail closed at startup)."""


def parse_service_credentials(raw: str, *, default_audience: str) -> tuple[ServiceCredential, ...]:
    """Parse the ``MAP_SERVICE_CREDENTIALS`` JSON array into credentials.

    Entry schema: ``key_id`` (required, valid id), ``token`` (required,
    non-empty), ``service_name`` (required, valid id), ``audience``
    (optional; defaults to ``default_audience``), ``scopes`` (comma
    separated string or list), ``revoked`` (optional bool). Duplicate
    ``key_id`` or duplicate ``token`` values are rejected: one token must
    map to exactly one identity. Error messages never echo token values.
    """
    raw = (raw or "").strip()
    if not raw:
        return ()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceCredentialConfigError(
            f"MAP_SERVICE_CREDENTIALS is not valid JSON: {exc.msg} at position {exc.pos}"
        ) from None
    if not isinstance(entries, list):
        raise ServiceCredentialConfigError("MAP_SERVICE_CREDENTIALS must be a JSON array")

    credentials: list[ServiceCredential] = []
    seen_key_ids: set[str] = set()
    seen_tokens: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"entry #{index}"
        if not isinstance(entry, dict):
            raise ServiceCredentialConfigError(f"{where}: must be a JSON object")
        key_id = str(entry.get("key_id") or "").strip()
        token = str(entry.get("token") or "")
        service_name = str(entry.get("service_name") or "").strip()
        audience = str(entry.get("audience") or "").strip() or default_audience
        raw_scopes = entry.get("scopes") or ""
        if isinstance(raw_scopes, list):
            raw_scopes = ",".join(str(item) for item in raw_scopes)
        revoked = bool(entry.get("revoked", False))

        if not is_valid_id(key_id):
            raise ServiceCredentialConfigError(f"{where}: key_id missing or invalid")
        if not token:
            raise ServiceCredentialConfigError(f"{where}: token missing")
        if not is_valid_id(service_name):
            raise ServiceCredentialConfigError(f"{where}: service_name missing or invalid")
        if not is_valid_id(audience):
            raise ServiceCredentialConfigError(f"{where}: audience invalid")
        if key_id in seen_key_ids:
            raise ServiceCredentialConfigError(f"{where}: duplicate key_id {key_id!r}")
        if token in seen_tokens:
            raise ServiceCredentialConfigError(f"{where}: duplicate token (key_id {key_id!r})")
        seen_key_ids.add(key_id)
        seen_tokens.add(token)
        credentials.append(
            ServiceCredential(
                key_id=key_id,
                token=token,
                service_name=service_name,
                audience=audience,
                scopes=_split_scopes(str(raw_scopes)),
                revoked=revoked,
            )
        )
    return tuple(credentials)


class ServiceAuthenticationError(Exception):
    """Token invalid / audience mismatch / missing service identity."""


def _match_credential(
    token: str, credentials: Sequence[ServiceCredential]
) -> ServiceCredential | None:
    """Find the live credential for ``token`` (constant-time per entry).

    Every registered token is compared so revoked entries stay
    indistinguishable from unknown ones by timing; revoked entries never
    match.
    """
    matched: ServiceCredential | None = None
    for credential in credentials:
        if constant_time_equal(token, credential.token):
            if credential.revoked:
                return None  # explicit revocation wins over any match
            matched = credential
    return matched


def _check_claim_headers(request: Request, credential: ServiceCredential) -> None:
    """Consistency-check ``X-Service-*`` headers against inherent claims.

    The headers are transport/debug information only: they never enlarge
    the grant. A contradiction (impersonated name, foreign audience,
    scopes beyond the credential grant) rejects the request fail-closed.
    """
    claimed_name = (request.headers.get("X-Service-Name") or "").strip()
    if claimed_name and claimed_name != credential.service_name:
        raise ServiceAuthenticationError(
            "X-Service-Name contradicts the credential's service_name",
            "INVALID_SERVICE_IDENTITY",
        )
    claimed_audience = (request.headers.get("X-Service-Audience") or "").strip()
    if claimed_audience and claimed_audience != credential.audience:
        raise ServiceAuthenticationError(
            "X-Service-Audience contradicts the credential's audience",
            "INVALID_SERVICE_IDENTITY",
        )
    granted = set(credential.scopes)
    claimed_scopes = _split_scopes(request.headers.get("X-Service-Scopes"))
    overclaimed = [scope for scope in claimed_scopes if scope not in granted]
    if overclaimed:
        raise ServiceAuthenticationError(
            "X-Service-Scopes exceed the credential's granted scopes",
            "FORBIDDEN",
        )


def authenticate_service(
    request: Request, *, credentials: Sequence[ServiceCredential]
) -> ServicePrincipal:
    """Validate the request as a service call, else raise 401/403-worthy error.

    The bearer token selects a registered credential; the returned
    principal carries ONLY that credential's inherent claims. Raises
    :class:`ServiceAuthenticationError` with a stable error code in
    ``args[1]``: ``INVALID_SERVICE_IDENTITY`` (bad/missing token,
    impersonation headers) or ``FORBIDDEN`` (valid token, scope overclaim
    or scope not granted).
    """
    auth_header = request.headers.get("Authorization", "")
    match = _TOKEN_RE.match(auth_header)
    if match is None:
        raise ServiceAuthenticationError("missing service bearer token", "INVALID_SERVICE_IDENTITY")
    credential = _match_credential(match.group(1), credentials)
    if credential is None:
        raise ServiceAuthenticationError("invalid service token", "INVALID_SERVICE_IDENTITY")

    _check_claim_headers(request, credential)
    return ServicePrincipal(
        service_name=credential.service_name,
        audience=credential.audience,
        scopes=credential.scopes,
        key_id=credential.key_id,
    )
