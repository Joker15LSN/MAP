"""S4-01: durable OpenSandbox execution capability.

The algorithm link reaches the OpenSandbox Server ONLY through this
production tool + OpenSandboxClient; there is NO host fallback. The tool
is registered in the shared tool registry and invoked by both engines
(legacy ToolCallAgent and the AgentScope adapter) through ToolExecutor.

S4-01 hardening (replaces the S3-01 in-process ledger):

- the durable identity chain (workspace/run/step/attempt/invocation/
  client_request) must arrive COMPLETE from the request path - missing
  fields fail closed with OPENSANDBOX_IDENTITY_INCOMPLETE, nothing is
  invented locally;
- exactly-once is enforced by a SandboxInvocationLedger keyed on
  (workspace_id, invocation_id). The atomic claim (a PostgreSQL unique
  constraint, or the equivalent in the in-memory double) guarantees ONE
  caller drives the remote create/execute; the others replay the recorded
  outcome. The ledger is the durable source of truth, never an in-process
  dict and never the destroyed remote sandbox;
- idempotency keys embed workspace + a normalized request digest, so the
  SAME invocation id with a DIFFERENT payload conflicts instead of
  replaying an old result;
- a lost create/execute response never blindly resends a mutation: the
  handler reconciles against the server WHILE the sandbox still exists
  and otherwise fails closed to an unknown terminal state.

Server contract: OpenSandbox Server 0.2.2 OpenAPI (PROTOCOL_VERSION);
the image reference pins the 0.2.2 tag. AC-SEC-12 remains blocked until
the real-server dedup acceptance runs.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .agent.base import AgentRequest, ToolResult
from .agent.tool_runtime import Tool
from .opensandbox_client import (
    MISSING_CONFIG_ERROR,
    UNKNOWN_OUTCOME,
    OpenSandboxClient,
    OpenSandboxClientError,
    SandboxIdentity,
    SandboxResourceLimits,
)
from .sandbox_ledger import (
    IDEMPOTENCY_CONFLICT,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    PostgresSandboxInvocationLedger,
    SandboxInvocationLedger,
    SandboxInvocationRecord,
    SandboxLedgerError,
    build_create_key,
    build_execute_key,
    normalize_request_digest,
)

CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
IDENTITY_INCOMPLETE = "OPENSANDBOX_IDENTITY_INCOMPLETE"
INVOCATION_IN_PROGRESS = "OPENSANDBOX_INVOCATION_IN_PROGRESS"
PROTOCOL_VERSION = "0.2.2"
SERVER_IMAGE_REF = "ghcr.io/opensandbox-group/opensandbox:0.2.2"

SANDBOX_TOOL_NAME = "sandbox_exec_tool"
SANDBOX_TOOL_DESCRIPTION = (
    "在远程 OpenSandbox 沙箱中执行一条命令（无宿主回退）。"
    "命令超时会先对账远端状态，绝不盲重放副作用。"
)

SANDBOX_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "要在沙箱中执行的命令（限制在资源配额内）",
        }
    },
    "required": ["command"],
}

# The durable identity fields the request path MUST supply. Anything
# missing fails closed instead of being invented.
REQUIRED_IDENTITY_FIELDS = (
    "workspace_id",
    "run_id",
    "step_id",
    "attempt_id",
    "invocation_id",
    "client_request_id",
)

# Ledger injection. Production resolves the PostgreSQL ledger lazily from
# POSTGRES_DSN; tests inject the in-memory double (same transactional
# semantics) via set_sandbox_ledger().
_ledger: SandboxInvocationLedger | None = None


def set_sandbox_ledger(ledger: SandboxInvocationLedger | None) -> None:
    """Inject the ledger dependency (tests / no-PG deployments)."""
    global _ledger
    _ledger = ledger


def reset_sandbox_ledger() -> None:
    """Reset the injected ledger so the production default is re-resolved."""
    set_sandbox_ledger(None)


def _get_sandbox_ledger() -> SandboxInvocationLedger:
    global _ledger
    if _ledger is not None:
        return _ledger
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    _ledger = PostgresSandboxInvocationLedger(dsn)
    return _ledger


def _identity_from_request(request: AgentRequest) -> SandboxIdentity | str:
    """Build the durable identity chain from the request runtime payload.

    Every field must already exist in the persisted execution context - a
    missing field returns the name of the missing field (fail-closed); the
    handler turns it into OPENSANDBOX_IDENTITY_INCOMPLETE.
    """
    extra = request.extra or {}
    missing = [name for name in REQUIRED_IDENTITY_FIELDS if not extra.get(name)]
    if missing:
        return ", ".join(sorted(missing))
    return SandboxIdentity(
        workspace_id=str(extra["workspace_id"]),
        run_id=str(extra["run_id"]),
        step_id=str(extra["step_id"]),
        attempt_id=str(extra["attempt_id"]),
        invocation_id=str(extra["invocation_id"]),
        client_request_id=str(extra["client_request_id"]),
    )


def _result_meta(
    *,
    identity: SandboxIdentity,
    limits: SandboxResourceLimits,
    server_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Durable record: identity, policy version, limits and remote state."""
    return {
        "source": "opensandbox",
        "protocol_version": PROTOCOL_VERSION,
        "server_image_ref": SERVER_IMAGE_REF,
        "identity": identity.to_dict(),
        "limits": limits.to_dict(),
        "server_state": server_state or {},
    }


def _prior_server_execution(
    state: dict[str, Any], execute_key: str
) -> dict[str, Any] | None:
    """Look up whether the SERVER already executed our execute key.

    Only valid while the sandbox still exists (before destroy). After the
    sandbox is destroyed the server state is no longer queryable and the
    LEDGER is the source of truth.
    """
    executions = state.get("executions") or []
    if not isinstance(executions, list):
        return None
    for execution in executions:
        if not isinstance(execution, dict):
            continue
        if execution.get("key") == execute_key:
            return execution
    return None


def _replay(
    record: SandboxInvocationRecord,
    identity: SandboxIdentity,
    limits: SandboxResourceLimits,
) -> ToolResult:
    """Replay a terminal ledger fact without touching the server."""
    if record.status == STATUS_SUCCEEDED:
        return ToolResult(
            name=SANDBOX_TOOL_NAME,
            content=record.output or "",
            data_source=_result_meta(
                identity=identity, limits=limits, server_state=record.server_state
            ),
        )
    error = record.error or f"OPENSANDBOX_{record.status.upper()}"
    return ToolResult(
        success=False,
        name=SANDBOX_TOOL_NAME,
        error=error,
        data_source=_result_meta(
            identity=identity, limits=limits, server_state=record.server_state
        ),
    )


async def _wait_for_terminal(
    ledger: SandboxInvocationLedger,
    workspace_id: str,
    invocation_id: str,
    timeout_s: float = 30.0,
) -> SandboxInvocationRecord | None:
    """Wait for the owning caller to settle the invocation, then replay.

    Never takes over and never re-issues a mutation: a caller that does not
    own the claim just waits for a terminal fact.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = await ledger.get(
            workspace_id=workspace_id, invocation_id=invocation_id
        )
        if record is not None and record.terminal:
            return record
        await asyncio.sleep(0.05)
    return None


async def _complete_best_effort(
    ledger: SandboxInvocationLedger,
    identity: SandboxIdentity,
    *,
    status: str,
    sandbox_id: str | None = None,
    output: str | None = None,
    error: str | None = None,
    server_state: dict[str, Any] | None = None,
) -> None:
    """Settle the invocation; a ledger failure must not mask the outcome."""
    try:
        await ledger.complete(
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            status=status,
            sandbox_id=sandbox_id,
            output=output,
            error=error,
            server_state=server_state,
        )
    except SandboxLedgerError:
        # The remote outcome already happened; never raise the ledger error
        # over a settled execution. The row stays pending/created for a later
        # operator reconciliation.
        pass


async def _sandbox_execute_handler(
    args: dict[str, Any], request: AgentRequest, parid: str
) -> ToolResult:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolResult(
            success=False,
            name=SANDBOX_TOOL_NAME,
            error="sandbox_exec_tool: command must be a non-empty string",
        )

    identity_result = _identity_from_request(request)
    if isinstance(identity_result, str):
        # Never invent identity - fail closed with a typed error.
        return ToolResult(
            success=False,
            name=SANDBOX_TOOL_NAME,
            error=(
                f"{IDENTITY_INCOMPLETE}: the request path must supply the "
                f"complete durable identity; missing: {identity_result}"
            ),
            data_source={
                "source": "opensandbox",
                "error_code": IDENTITY_INCOMPLETE,
            },
        )
    identity = identity_result

    limits = SandboxResourceLimits()
    request_digest = normalize_request_digest(
        command=command, limits=limits.to_dict()
    )
    create_key = build_create_key(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=request_digest,
    )
    execute_key = build_execute_key(
        workspace_id=identity.workspace_id,
        invocation_id=identity.invocation_id,
        request_digest=request_digest,
    )

    ledger = _get_sandbox_ledger()
    try:
        claim = await ledger.claim(
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
        )
    except SandboxLedgerError as exc:
        return ToolResult(
            success=False,
            name=SANDBOX_TOOL_NAME,
            error=str(exc),
            data_source={"source": "opensandbox", "error_code": exc.code},
        )

    if claim.kind == "conflict":
        return ToolResult(
            success=False,
            name=SANDBOX_TOOL_NAME,
            error=(
                f"{IDEMPOTENCY_CONFLICT}: invocation {identity.invocation_id!r} "
                f"in workspace {identity.workspace_id!r} was already used with "
                "a different request payload; it will not be replayed"
            ),
            data_source={"source": "opensandbox", "error_code": IDEMPOTENCY_CONFLICT},
        )
    if claim.kind == "replay":
        return _replay(claim.record, identity, limits)  # type: ignore[arg-type]
    if claim.kind == "in_progress":
        record = await _wait_for_terminal(
            ledger, identity.workspace_id, identity.invocation_id
        )
        if record is None:
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error=(
                    f"{INVOCATION_IN_PROGRESS}: invocation "
                    f"{identity.invocation_id!r} is owned by another caller and "
                    "did not reach a terminal state in time; refusing to "
                    "re-issue the mutation"
                ),
                data_source={
                    "source": "opensandbox",
                    "error_code": INVOCATION_IN_PROGRESS,
                },
            )
        return _replay(record, identity, limits)

    # Owned: this caller drives the remote create/execute exactly once.
    try:
        return await _drive_remote_execution(
            command=command,
            identity=identity,
            limits=limits,
            create_key=create_key,
            execute_key=execute_key,
            ledger=ledger,
        )
    except OpenSandboxClientError as exc:
        # Definitive failure (unreachable / API error): settle the row so a
        # retry replays the same terminal fact, then propagate.
        await _complete_best_effort(
            ledger, identity, status=STATUS_FAILED, error=str(exc)
        )
        raise
    except Exception as exc:  # noqa: BLE001 - handler boundary
        await _complete_best_effort(
            ledger, identity, status=STATUS_FAILED, error=str(exc)
        )
        raise


async def _drive_remote_execution(
    *,
    command: str,
    identity: SandboxIdentity,
    limits: SandboxResourceLimits,
    create_key: str,
    execute_key: str,
    ledger: SandboxInvocationLedger,
) -> ToolResult:
    """The one caller that owns the claim performs create/execute/destroy."""
    try:
        client = OpenSandboxClient.from_env()
    except OpenSandboxClientError as exc:
        if exc.code == MISSING_CONFIG_ERROR:
            # No host fallback: the capability is simply disabled.
            await _complete_best_effort(
                ledger,
                identity,
                status=STATUS_FAILED,
                error=f"{CAPABILITY_DISABLED}: {exc}",
            )
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error=f"{CAPABILITY_DISABLED}: {exc}",
                data_source={"source": "opensandbox", "error_code": exc.code},
            )
        raise

    sandbox_id: str | None = None
    async with client:
        # Create. A lost create response is NOT blind-retried: without a
        # server-side dedup guarantee the outcome is unknown and fail-closed.
        try:
            created = await client.create_sandbox(
                identity, limits, idempotency_key=create_key
            )
        except OpenSandboxClientError as exc:
            if exc.code == UNKNOWN_OUTCOME:
                error = (
                    "OPENSANDBOX_UNKNOWN_OUTCOME: create timed out and the "
                    "server dedup guarantee is unverified; refusing to "
                    "blindly re-send the mutation"
                )
                await _complete_best_effort(
                    ledger, identity, status=STATUS_UNKNOWN, error=error
                )
                return ToolResult(
                    success=False,
                    name=SANDBOX_TOOL_NAME,
                    error=error,
                    data_source={"source": "opensandbox", "error_code": UNKNOWN_OUTCOME},
                )
            raise
        sandbox_id = str(created.get("sandbox_id") or "")
        if not sandbox_id:
            error = "OPENSANDBOX_API_ERROR: create response missing sandbox_id"
            await _complete_best_effort(
                ledger, identity, status=STATUS_FAILED, error=error
            )
            return ToolResult(
                success=False, name=SANDBOX_TOOL_NAME, error=error
            )

        await ledger.record_created(
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            sandbox_id=sandbox_id,
        )

        # Reconcile BEFORE mutating (sandbox still exists): if the server
        # already executed our key, take its result instead of re-issuing.
        try:
            state = await client.get_sandbox(sandbox_id)
        except OpenSandboxClientError:
            state = {}
        prior = _prior_server_execution(state, execute_key)
        if prior is not None:
            output = str(prior.get("output") or "")
            await _complete_best_effort(
                ledger,
                identity,
                status=STATUS_SUCCEEDED,
                sandbox_id=sandbox_id,
                output=output,
                server_state=state,
            )
            await _destroy_best_effort(client, sandbox_id)
            return ToolResult(
                name=SANDBOX_TOOL_NAME,
                content=output,
                data_source=_result_meta(
                    identity=identity, limits=limits, server_state=state
                ),
            )

        try:
            outcome = await client.execute(
                sandbox_id,
                identity,
                command,
                limits.timeout_seconds,
                idempotency_key=execute_key,
            )
        except OpenSandboxClientError as exc:
            if exc.code == UNKNOWN_OUTCOME:
                # Reconcile while the sandbox still exists; never blind-replay.
                try:
                    state = await client.reconcile(sandbox_id)
                except OpenSandboxClientError as reconcile_exc:
                    error = (
                        "OPENSANDBOX_UNKNOWN_OUTCOME: execute timed out and "
                        f"reconciliation failed: {reconcile_exc}"
                    )
                    await _complete_best_effort(
                        ledger, identity, status=STATUS_UNKNOWN, error=error
                    )
                    return ToolResult(
                        success=False,
                        name=SANDBOX_TOOL_NAME,
                        error=error,
                        data_source={
                            "source": "opensandbox",
                            "error_code": UNKNOWN_OUTCOME,
                        },
                    )
                prior = _prior_server_execution(state, execute_key)
                if prior is not None:
                    output = str(prior.get("output") or "")
                    await _complete_best_effort(
                        ledger,
                        identity,
                        status=STATUS_SUCCEEDED,
                        sandbox_id=sandbox_id,
                        output=output,
                        server_state=state,
                    )
                    await _destroy_best_effort(client, sandbox_id)
                    return ToolResult(
                        name=SANDBOX_TOOL_NAME,
                        content=output,
                        data_source=_result_meta(
                            identity=identity, limits=limits, server_state=state
                        ),
                    )
                error = (
                    "OPENSANDBOX_UNKNOWN_OUTCOME: execute timed out; "
                    "server-side state reconciled - no duplicate execution "
                    "was issued"
                )
                await _complete_best_effort(
                    ledger,
                    identity,
                    status=STATUS_UNKNOWN,
                    sandbox_id=sandbox_id,
                    error=error,
                    server_state=state,
                )
                return ToolResult(
                    success=False,
                    name=SANDBOX_TOOL_NAME,
                    error=error,
                    data_source=_result_meta(
                        identity=identity, limits=limits, server_state=state
                    ),
                )
            raise

        output = str(outcome.get("output") or outcome.get("result") or "")

        # Record the terminal success BEFORE destroy: the ledger is the
        # durable source of truth once the sandbox is gone and the server is
        # no longer queryable.
        await _complete_best_effort(
            ledger,
            identity,
            status=STATUS_SUCCEEDED,
            sandbox_id=sandbox_id,
            output=output,
            server_state=outcome,
        )
        await _destroy_best_effort(client, sandbox_id)
        return ToolResult(
            name=SANDBOX_TOOL_NAME,
            content=output,
            data_source=_result_meta(
                identity=identity, limits=limits, server_state=outcome
            ),
        )


async def _destroy_best_effort(client: OpenSandboxClient, sandbox_id: str) -> None:
    # Best-effort teardown; the server also enforces sandbox TTL.
    try:
        await client.destroy_sandbox(sandbox_id)
    except OpenSandboxClientError:
        pass


def build_sandbox_tools() -> dict[str, Tool]:
    """The production execution tool (no host fallback)."""
    return {
        SANDBOX_TOOL_NAME: Tool(
            name=SANDBOX_TOOL_NAME,
            description=SANDBOX_TOOL_DESCRIPTION,
            parameters=SANDBOX_TOOL_PARAMETERS,
            handler=_sandbox_execute_handler,
        )
    }
