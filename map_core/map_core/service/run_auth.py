"""Step 8 PR-K5: internal service authentication for the typed run stream.

``POST /internal/v1/runs/{run_id}/attempts/{attempt}/events`` is the
service-to-service replacement for the legacy chat stream.  It is a
privileged, durable-run execution surface: the worker presents a
deployment-injected credential whose token matches (constant time), whose
audience equals the configured run-service audience, whose temporal validity
window (not_before/expires_at) covers now, and whose scopes include
``runs.execute``.  The correlation headers remain caller-chosen and can
never be an authorization basis.

Credentials come exclusively from the ``MAP_RUN_SERVICE_CREDENTIALS``
environment variable (JSON array of ``{key_id, token, service_name,
audience, scopes, expires_at[, not_before]}``) injected by the deployment
secret provider.  The same S7-04 fail-closed rules as the sandbox registry
apply: ``audience`` and ``expires_at`` are mandatory per credential, missing
or invalid values are a configuration error, never a wider grant.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

RUN_EXECUTE_SCOPE = "runs.execute"
RUN_AUDIENCE_DEFAULT = "map-core"

CREDENTIALS_ENV = "MAP_RUN_SERVICE_CREDENTIALS"
AUDIENCE_ENV = "MAP_RUN_SERVICE_AUDIENCE"


def run_audience() -> str:
    return (
        os.getenv(AUDIENCE_ENV, RUN_AUDIENCE_DEFAULT) or ""
    ).strip() or RUN_AUDIENCE_DEFAULT


def _parse_instant(value: str, field: str) -> datetime:
    """Parse an ISO-8601 UTC instant (``Z`` or ``+00:00`` suffix).

    Fail-closed: anything that is not a real instant raises ValueError so a
    broken credential registry can never silently widen access.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"credential field {field} must be a non-empty timestamp")
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"credential field {field} is not a valid ISO-8601 instant: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"credential field {field} must be timezone-aware (UTC): {raw!r}"
        )
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RunServiceCredential:
    key_id: str
    token: str
    service_name: str
    audience: str
    scopes: frozenset[str]
    expires_at: datetime
    not_before: datetime | None = None

    def valid_at(self, now: datetime) -> bool:
        """Temporal validity: not_before <= now < expires_at."""
        return (self.not_before is None or now >= self.not_before) and now < self.expires_at


def parse_run_credentials(raw: str | None) -> tuple[RunServiceCredential, ...]:
    """Parse the deployment-injected run-service credential registry.

    An absent registry yields no credentials: EVERY request is rejected
    with 401 - the typed run stream is disabled until the secret provider
    injects at least one credential.  Malformed JSON / entries raise
    ValueError so a broken registry can never silently widen access.

    Every credential must explicitly carry a non-empty ``audience`` (never
    inherited from ``MAP_RUN_SERVICE_AUDIENCE``) and an ``expires_at``
    timestamp.  ``not_before`` is optional; absent means valid immediately.
    """
    if not raw or not raw.strip():
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("MAP_RUN_SERVICE_CREDENTIALS must be a JSON array")
    credentials: list[RunServiceCredential] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError("credential entries must be JSON objects")
        token = str(entry.get("token") or "").strip()
        key_id = str(entry.get("key_id") or "").strip()
        service_name = str(entry.get("service_name") or "").strip()
        audience = str(entry.get("audience") or "").strip()
        if not token or not key_id or not service_name:
            raise ValueError(
                "each credential requires token, key_id and service_name"
            )
        if not audience:
            raise ValueError(
                "each credential requires an explicit non-empty audience "
                "(the deployment default is never inherited)"
            )
        if entry.get("expires_at") in (None, ""):
            raise ValueError(
                "each credential requires expires_at "
                f"(credential #{index} has no expiration)"
            )
        expires_at = _parse_instant(str(entry.get("expires_at")), "expires_at")
        not_before = None
        if entry.get("not_before") not in (None, ""):
            not_before = _parse_instant(str(entry.get("not_before")), "not_before")
        scopes = frozenset(
            str(scope).strip() for scope in (entry.get("scopes") or [])
        )
        credentials.append(
            RunServiceCredential(
                key_id=key_id,
                token=token,
                service_name=service_name,
                audience=audience,
                scopes=scopes,
                expires_at=expires_at,
                not_before=not_before,
            )
        )
    return tuple(credentials)


def authenticate_run_request(
    authorization: str | None,
    credentials: tuple[RunServiceCredential, ...],
    now: datetime | None = None,
) -> tuple[RunServiceCredential | None, str | None]:
    """Authorize a run-stream request against the injected registry.

    Returns (credential, None) when ALL of the following hold:
    - a Bearer token is presented and matches a registered token in
      CONSTANT time (no oracle for token prefixes);
    - the credential is temporally valid at ``now``
      (not_before <= now < expires_at; expired / not-yet-valid -> 401);
    - the credential's audience equals the configured run-service audience;
    - the credential carries the runs.execute scope.

    Returns (None, reason) otherwise, where reason distinguishes:
    - "missing": no Bearer token presented, nothing registered, no
      registered token matches, or the matched credential is outside its
      temporal validity window -> 401;
    - "forbidden": a REGISTERED, temporally-valid token matched but its
      audience or scope is wrong -> 403.
    """
    current = now or _now()
    if not authorization or not authorization.lower().startswith("bearer "):
        return None, "missing"
    presented = authorization[len("Bearer "):].strip()
    if not presented:
        return None, "missing"
    audience = run_audience()
    presented_bytes = presented.encode("utf-8")
    for credential in credentials:
        if not hmac.compare_digest(
            credential.token.encode("utf-8"), presented_bytes
        ):
            continue
        if not credential.valid_at(current):
            return None, "missing"
        if credential.audience != audience:
            return None, "forbidden"
        if RUN_EXECUTE_SCOPE not in credential.scopes:
            return None, "forbidden"
        return credential, None
    return None, "missing"
