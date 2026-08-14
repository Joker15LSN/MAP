"""Authenticated OpenSandbox HTTP client (P0-SEC-01, review R-02).

The algorithm link calls the OpenSandbox Server exclusively over
authenticated asynchronous HTTP; map_core never mounts a docker socket or
kubeconfig and never falls back to host execution. Every request carries the
durable identity chain (workspace_id/run_id/step_id/attempt_id/
invocation_id/client_request_id) plus a request idempotency key, so a lost
response can be replayed and reconciled without duplicating side effects.

Endpoint paths follow the OpenSandbox Server 0.2.2 OpenAPI contract and are
finalized in the AC-SEC-12 integration acceptance against the real server;
the client itself is contract-tested against a local mock transport.
Secrets never appear in logs, exceptions or repr output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

import httpx

CONNECT_TIMEOUT_S: Final[float] = 2.0  # profile: connect_timeout_ms 2000
CONTROL_TIMEOUT_S: Final[float] = 10.0  # profile: control_request_timeout_ms 10000
CREATE_TIMEOUT_S: Final[float] = 60.0  # profile: create_request_timeout_ms 60000

AUTH_HEADER: Final[str] = "OPEN-SANDBOX-API-KEY"
IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"

MISSING_CONFIG_ERROR = "OPENSANDBOX_CONFIG_MISSING"
CONNECT_ERROR = "OPENSANDBOX_UNREACHABLE"
API_ERROR = "OPENSANDBOX_API_ERROR"
UNKNOWN_OUTCOME = "OPENSANDBOX_UNKNOWN_OUTCOME"


class OpenSandboxClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SandboxIdentity:
    """Durable identity chain required on every execution request."""

    workspace_id: str
    run_id: str
    step_id: str
    attempt_id: str
    invocation_id: str
    client_request_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "attempt_id": self.attempt_id,
            "invocation_id": self.invocation_id,
            "client_request_id": self.client_request_id,
        }


@dataclass(frozen=True)
class SandboxResourceLimits:
    cpu_seconds: int = 30
    memory_mb: int = 512
    disk_mb: int = 1024
    max_output_bytes: int = 64 * 1024
    timeout_seconds: int = 30

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "memory_mb": self.memory_mb,
            "disk_mb": self.disk_mb,
            "max_output_bytes": self.max_output_bytes,
            "timeout_seconds": self.timeout_seconds,
        }


class OpenSandboxClient:
    """Async HTTP client for one OpenSandbox Server instance.

    The api key is used only to sign the auth header. Logging/repr never
    include it; callers record only the redacted header map
    (safe_headers()) in logs, traces and result summaries.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(CONTROL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> "OpenSandboxClient":
        base_url = (os.getenv("MAP_OPENSANDBOX_URL") or "").strip()
        api_key = (os.getenv("MAP_OPENSANDBOX_API_KEY") or "").strip()
        if not base_url or not api_key:
            raise OpenSandboxClientError(
                MISSING_CONFIG_ERROR,
                "MAP_OPENSANDBOX_URL and MAP_OPENSANDBOX_API_KEY are required "
                "to use the OpenSandbox capability (fail-closed)",
            )
        return cls(base_url=base_url, api_key=api_key)

    def __repr__(self) -> str:
        # Never surface the api key in repr/log output.
        return (
            f"OpenSandboxClient(base_url={self._base_url!r}, "
            f"api_key=<redacted>)"
        )

    def safe_headers(self) -> dict[str, str]:
        """Headers as they may be recorded in logs/traces (key redacted)."""
        return {AUTH_HEADER: "<redacted>", "Authorization": "<redacted>"}

    def _headers(self, identity: SandboxIdentity | None = None) -> dict[str, str]:
        headers = {AUTH_HEADER: self._api_key, "Accept": "application/json"}
        if identity is not None:
            headers[IDEMPOTENCY_HEADER] = identity.client_request_id
        return headers

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenSandboxClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def health(self) -> bool:
        try:
            response = await self._client.get("/health", headers=self._headers())
        except httpx.HTTPError as exc:
            raise OpenSandboxClientError(
                CONNECT_ERROR, f"OpenSandbox health probe failed: {exc}"
            ) from exc
        return response.status_code == 200

    async def create_sandbox(
        self,
        identity: SandboxIdentity,
        limits: SandboxResourceLimits | None = None,
    ) -> dict[str, Any]:
        """Create a sandbox bound to the identity chain (idempotent).

        The request idempotency key is the client_request_id: a retried
        create with the same key returns the same sandbox (server contract,
        verified in AC-SEC-12). The returned payload must include the
        remote sandbox_id which the caller persists.
        """
        payload: dict[str, Any] = {
            **identity.to_dict(),
            "limits": (limits or SandboxResourceLimits()).to_dict(),
        }
        try:
            response = await self._client.post(
                "/api/v1/sandboxes",
                json=payload,
                headers=self._headers(identity),
                timeout=CREATE_TIMEOUT_S,
            )
        except httpx.TimeoutException as exc:
            raise OpenSandboxClientError(
                UNKNOWN_OUTCOME,
                "create timed out; reconcile via get_sandbox before retrying",
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenSandboxClientError(
                CONNECT_ERROR, f"OpenSandbox create failed: {exc}"
            ) from exc
        if response.status_code not in {200, 201}:
            raise OpenSandboxClientError(
                API_ERROR,
                f"create returned HTTP {response.status_code}",
                status=response.status_code,
            )
        data = response.json()
        if not isinstance(data, dict) or not data.get("sandbox_id"):
            raise OpenSandboxClientError(
                API_ERROR, "create response missing sandbox_id"
            )
        return data

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Remote status query used for reconciliation after timeouts."""
        try:
            response = await self._client.get(
                f"/api/v1/sandboxes/{sandbox_id}", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise OpenSandboxClientError(
                CONNECT_ERROR, f"OpenSandbox status query failed: {exc}"
            ) from exc
        if response.status_code != 200:
            raise OpenSandboxClientError(
                API_ERROR,
                f"status query returned HTTP {response.status_code}",
                status=response.status_code,
            )
        return response.json()

    async def execute(
        self,
        sandbox_id: str,
        identity: SandboxIdentity,
        command: str,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        payload = {
            "sandbox_id": sandbox_id,
            "command": command,
            "timeout_seconds": timeout_seconds,
            **identity.to_dict(),
        }
        try:
            response = await self._client.post(
                f"/api/v1/sandboxes/{sandbox_id}/execute",
                json=payload,
                headers=self._headers(identity),
                timeout=max(CONTROL_TIMEOUT_S, timeout_seconds + 5),
            )
        except httpx.TimeoutException as exc:
            raise OpenSandboxClientError(
                UNKNOWN_OUTCOME,
                "execute timed out; reconcile via get_sandbox before retrying",
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenSandboxClientError(
                CONNECT_ERROR, f"OpenSandbox execute failed: {exc}"
            ) from exc
        if response.status_code not in {200, 201}:
            raise OpenSandboxClientError(
                API_ERROR,
                f"execute returned HTTP {response.status_code}",
                status=response.status_code,
            )
        return response.json()

    async def destroy_sandbox(self, sandbox_id: str) -> bool:
        try:
            response = await self._client.delete(
                f"/api/v1/sandboxes/{sandbox_id}", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise OpenSandboxClientError(
                CONNECT_ERROR, f"OpenSandbox destroy failed: {exc}"
            ) from exc
        return response.status_code in {200, 204}

    async def reconcile(self, sandbox_id: str) -> dict[str, Any]:
        """Query the remote state after an uncertain outcome.

        Returns the server-side state snapshot. Callers use the snapshot's
        status to decide resume/retry and must not re-run a mutation whose
        server-side state already shows completion (no duplicate effects).
        """
        return await self.get_sandbox(sandbox_id)
