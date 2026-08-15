"""S6-03: internal service authentication for the sandbox execution endpoint.

POST /sandbox/exec is a privileged, deterministic command-execution surface.
The six identity headers are CORRELATION and IDEMPOTENCY only - they are
caller-chosen and can never be an authorization basis. A request is
authorized only when it presents a deployment-injected credential whose
token matches (constant time), whose audience equals the configured
service audience, and whose scopes include sandbox:execute.

Credentials come exclusively from the MAP_SANDBOX_SERVICE_CREDENTIALS
environment variable (JSON array of {key_id, token, service_name,
audience, scopes}) injected by the deployment secret provider - never from
the repository and never from request headers.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass

SANDBOX_EXEC_SCOPE = "sandbox:execute"
SANDBOX_AUDIENCE_DEFAULT = "map-core"

CREDENTIALS_ENV = "MAP_SANDBOX_SERVICE_CREDENTIALS"
AUDIENCE_ENV = "MAP_SANDBOX_SERVICE_AUDIENCE"


@dataclass(frozen=True)
class SandboxServiceCredential:
    key_id: str
    token: str
    service_name: str
    audience: str
    scopes: frozenset[str]


def sandbox_audience() -> str:
    return (os.getenv(AUDIENCE_ENV, SANDBOX_AUDIENCE_DEFAULT) or "").strip()         or SANDBOX_AUDIENCE_DEFAULT


def parse_sandbox_credentials(raw: str | None) -> tuple[SandboxServiceCredential, ...]:
    """Parse the deployment-injected credential registry (fail-closed).

    An absent registry yields no credentials: EVERY request is rejected
    with 401 - the capability is disabled until the secret provider
    injects at least one credential. Malformed JSON / entries raise
    ValueError so a broken registry can never silently widen access.
    """
    if not raw or not raw.strip():
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("MAP_SANDBOX_SERVICE_CREDENTIALS must be a JSON array")
    credentials: list[SandboxServiceCredential] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("credential entries must be JSON objects")
        token = str(entry.get("token") or "").strip()
        key_id = str(entry.get("key_id") or "").strip()
        service_name = str(entry.get("service_name") or "").strip()
        if not token or not key_id or not service_name:
            raise ValueError(
                "each credential requires token, key_id and service_name"
            )
        scopes = frozenset(
            str(scope).strip() for scope in (entry.get("scopes") or [])
        )
        credentials.append(
            SandboxServiceCredential(
                key_id=key_id,
                token=token,
                service_name=service_name,
                audience=str(entry.get("audience") or sandbox_audience()).strip()
                or sandbox_audience(),
                scopes=scopes,
            )
        )
    return tuple(credentials)


def authenticate_sandbox_request(
    authorization: str | None,
    credentials: tuple[SandboxServiceCredential, ...],
) -> tuple[SandboxServiceCredential | None, str | None]:
    """Authorize a request against the injected credential registry.

    Returns (credential, None) when ALL of the following hold:
    - a Bearer token is presented and matches a registered token in
      CONSTANT time (no oracle for token prefixes);
    - the credential's audience equals the configured service audience;
    - the credential carries the sandbox:execute scope.

    Returns (None, reason) otherwise, where reason distinguishes:
    - "missing": no Bearer token presented, nothing registered, or no
      registered token matches (forged / rotated-out credentials) -> 401;
    - "forbidden": a REGISTERED token matched but its audience or scope
      is wrong -> 403.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None, "missing"
    presented = authorization[len("Bearer "):].strip()
    if not presented:
        return None, "missing"
    audience = sandbox_audience()
    presented_bytes = presented.encode("utf-8")
    for credential in credentials:
        if not hmac.compare_digest(
            credential.token.encode("utf-8"), presented_bytes
        ):
            continue
        if credential.audience != audience:
            return None, "forbidden"
        if SANDBOX_EXEC_SCOPE not in credential.scopes:
            return None, "forbidden"
        return credential, None
    return None, "missing"
