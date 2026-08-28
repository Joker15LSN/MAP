"""BFF -> Core remote-owned seam for sandbox invocations (Step 3 / PR-E).

The port is :class:`SandboxRemote`. Two adapters exist from day one:

- :class:`HttpSandboxRemote` - production transport. It owns ALL knowledge
  of the Core sandbox wire shape (endpoint paths ``/sandbox/exec`` and
  ``/sandbox/reconcile``, the six identity headers, service identity error
  projection). Nothing else in the BFF may hand-build those headers or
  interpret Core sandbox response codes.
- :class:`InMemorySandboxRemote` - deterministic scripted adapter used by
  contract tests; it replays :class:`SandboxExecutionResult` values and can
  deduplicate by execute key or inject ``unknown`` outcomes.

Core never writes Run/Event PG (ADR-0002): the RunWorker persists the
durable ``effect.*`` facts around this remote call.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from ..core_client import MapCoreClient

_SANDBOX_EXEC_PATH = "/sandbox/exec"
_SANDBOX_RECONCILE_PATH = "/sandbox/reconcile"

_CORE_AUTH_ERROR = "OPENSANDBOX_UNAUTHORIZED"
_CORE_FORBIDDEN_ERROR = "OPENSANDBOX_FORBIDDEN"
_CORE_UNKNOWN_OUTCOME = "OPENSANDBOX_UNKNOWN_OUTCOME"


@dataclass(frozen=True)
class SandboxIdentity:
    """Durable identity chain for one sandbox invocation (F-04 ID rules)."""

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
class SandboxExecutionRequest:
    """Everything the remote Core needs to execute (or resume) one sandbox
    invocation. The keys are stable idempotency keys created by the Run
    effect rules; they are never invented inside an adapter."""

    identity: SandboxIdentity
    command: str
    limits: dict[str, int]
    create_key: str
    execute_key: str
    resume_sandbox_id: str | None = None
    resume_phase: str | None = None


@dataclass(frozen=True)
class SandboxReference:
    """Reference used to reconcile a previously-uncertain invocation."""

    identity: SandboxIdentity
    sandbox_id: str
    execute_key: str
    resume_phase: str | None = None
    create_key: str | None = None


@dataclass(frozen=True)
class SandboxExecutionResult:
    """Typed outcome of a remote sandbox execution.

    ``success`` is true only when ``status == "succeeded"``; an ``unknown``
    outcome is never reported as success (the worker must keep it uncertain).
    """

    success: bool
    status: Literal["succeeded", "failed", "unknown"]
    sandbox_id: str | None = None
    output: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    server_state: dict[str, Any] | None = None


class SandboxRemote(Protocol):
    async def execute(
        self, request: SandboxExecutionRequest
    ) -> SandboxExecutionResult: ...

    async def reconcile(
        self, reference: SandboxReference
    ) -> SandboxExecutionResult: ...


def _error_code_from_response(
    body: dict[str, Any], *, status_code: int | None
) -> str:
    data_source = body.get("data_source")
    if isinstance(data_source, dict):
        code = data_source.get("error_code")
        if isinstance(code, str) and code:
            return code
    code = body.get("error_code")
    if isinstance(code, str) and code:
        return code
    if status_code == 401:
        return _CORE_AUTH_ERROR
    if status_code == 403:
        return _CORE_FORBIDDEN_ERROR
    return "OPENSANDBOX_FAILED"


class HttpSandboxRemote:
    """Production adapter: Core sandbox HTTP -> typed result.

    This class is the only BFF place that knows ``/sandbox/exec``,
    ``/sandbox/reconcile``, the six identity headers and the projection of
    Core's success=false contract (``data_source.error_code``).
    """

    def __init__(self, client: MapCoreClient, *, core_token: str | None = None) -> None:
        self._client = client
        self._core_token = (core_token or "").strip()

    def _headers(self, identity: SandboxIdentity) -> dict[str, str]:
        headers = {
            "X-Workspace-ID": identity.workspace_id,
            "X-Run-ID": identity.run_id,
            "X-Step-ID": identity.step_id,
            "X-Attempt-ID": identity.attempt_id,
            "X-Invocation-ID": identity.invocation_id,
            "X-Client-Request-ID": identity.client_request_id,
        }
        if self._core_token:
            headers["Authorization"] = f"Bearer {self._core_token}"
        return headers

    async def _post(
        self, path: str, payload: dict[str, Any], *, identity: SandboxIdentity
    ) -> SandboxExecutionResult:
        try:
            body = await self._client.chat_by_path(
                path, payload, self._headers(identity)
            )
        except httpx.HTTPStatusError as exc:
            # Project service-identity failures without leaking httpx or the
            # raw Core response body. 401/403 are terminal configuration
            # failures, never an "unknown" execution outcome.
            status_code = exc.response.status_code
            raw_body: dict[str, Any] = {}
            try:
                parsed = json.loads(exc.response.text or "{}")
                if isinstance(parsed, dict):
                    raw_body = parsed
            except (json.JSONDecodeError, ValueError):
                raw_body = {}
            return SandboxExecutionResult(
                success=False,
                status="failed",
                error_code=_error_code_from_response(
                    raw_body, status_code=status_code
                ),
                error_message=(
                    raw_body.get("detail")
                    or f"core sandbox endpoint returned HTTP {status_code}"
                ),
            )
        if not isinstance(body, dict):
            return SandboxExecutionResult(
                success=False,
                status="failed",
                error_code="OPENSANDBOX_FAILED",
                error_message="core sandbox endpoint returned a non-object body",
            )
        return self._project_result(body)

    def _project_result(self, body: dict[str, Any]) -> SandboxExecutionResult:
        success = body.get("success") is True
        data_source = body.get("data_source")
        error_code = None
        server_state: dict[str, Any] | None = None
        if isinstance(data_source, dict):
            raw_code = data_source.get("error_code")
            if isinstance(raw_code, str):
                error_code = raw_code
            raw_state = data_source.get("server_state")
            if isinstance(raw_state, dict):
                server_state = raw_state
        raw_state = body.get("server_state")
        if isinstance(raw_state, dict):
            server_state = raw_state
        if success:
            status = "succeeded"
        elif error_code == _CORE_UNKNOWN_OUTCOME:
            status = "unknown"
        else:
            status = "failed"
        sandbox_id = body.get("sandbox_id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            sandbox_id = None
        output = body.get("output")
        if not isinstance(output, str):
            output = body.get("content")
        if not isinstance(output, str):
            output = ""
        error_message = body.get("error")
        if not isinstance(error_message, str):
            error_message = None
        return SandboxExecutionResult(
            success=status == "succeeded",
            status=status,
            sandbox_id=sandbox_id,
            output=output,
            error_code=error_code,
            error_message=error_message,
            server_state=server_state,
        )

    async def execute(
        self, request: SandboxExecutionRequest
    ) -> SandboxExecutionResult:
        payload: dict[str, Any] = {
            "command": request.command,
            "limits": request.limits,
            "create_key": request.create_key,
            "execute_key": request.execute_key,
        }
        if request.resume_sandbox_id is not None:
            payload["resume_sandbox_id"] = request.resume_sandbox_id
        if request.resume_phase is not None:
            payload["resume_phase"] = request.resume_phase
        return await self._post(
            _SANDBOX_EXEC_PATH, payload, identity=request.identity
        )

    async def reconcile(
        self, reference: SandboxReference
    ) -> SandboxExecutionResult:
        payload: dict[str, Any] = {
            "sandbox_id": reference.sandbox_id,
            "execute_key": reference.execute_key,
            "resume_phase": reference.resume_phase,
            **reference.identity.to_dict(),
        }
        if reference.create_key is not None:
            payload["create_key"] = reference.create_key
        return await self._post(
            _SANDBOX_RECONCILE_PATH, payload, identity=reference.identity
        )


class InMemorySandboxRemote:
    """Deterministic adapter: scripted or keyed results.

    ``set_result`` deduplicates by execute key: the same key always replays
    the same result. ``set_unknown`` injects an unknown outcome for a key.
    Without a script or keyed result the adapter returns a default success
    so happy-path tests stay terse.
    """

    def __init__(self, script: Iterable[SandboxExecutionResult] | None = None) -> None:
        self._script = list(script or [])
        self._keyed: dict[str, SandboxExecutionResult] = {}
        self.execute_calls: list[SandboxExecutionRequest] = []
        self.reconcile_calls: list[SandboxReference] = []

    def set_result(self, execute_key: str, result: SandboxExecutionResult) -> None:
        self._keyed[execute_key] = result

    def set_unknown(self, execute_key: str) -> None:
        self._keyed[execute_key] = SandboxExecutionResult(
            success=False,
            status="unknown",
            error_code="OPENSANDBOX_UNKNOWN_OUTCOME",
            error_message="injected unknown outcome",
        )

    def _next_result(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        if self._script:
            return self._script.pop(0)
        return self._keyed.get(
            request.execute_key,
            SandboxExecutionResult(
                success=True,
                status="succeeded",
                sandbox_id="sb-in-memory",
                output="ok",
                server_state={"status": "completed"},
            ),
        )

    async def execute(
        self, request: SandboxExecutionRequest
    ) -> SandboxExecutionResult:
        self.execute_calls.append(request)
        return self._next_result(request)

    async def reconcile(
        self, reference: SandboxReference
    ) -> SandboxExecutionResult:
        self.reconcile_calls.append(reference)
        if self._script:
            return self._script.pop(0)
        return self._keyed.get(
            reference.execute_key,
            SandboxExecutionResult(
                success=True,
                status="succeeded",
                sandbox_id=reference.sandbox_id,
                output="ok",
                server_state={"status": "completed"},
            ),
        )


__all__ = [
    "HttpSandboxRemote",
    "InMemorySandboxRemote",
    "SandboxExecutionRequest",
    "SandboxExecutionResult",
    "SandboxIdentity",
    "SandboxReference",
    "SandboxRemote",
]
