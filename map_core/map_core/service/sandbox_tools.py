"""S3-01: production wiring of the OpenSandbox execution capability.

The algorithm link reaches the OpenSandbox Server ONLY through this
production tool + :class:`OpenSandboxClient`; there is NO host fallback.
The tool is registered in the shared tool registry by
:class:`AgentDispatcher` (``_register_dynamic_tools``) and listed in the
single capability schema (``find_invalid_tool_names`` accepts it), so both
engines (legacy ToolCallAgent and the AgentScope adapter) invoke the same
handler.

S3-01 hardening:

- the durable identity chain (workspace/run/step/attempt/invocation) must
  arrive COMPLETE from the Run worker/BFF - missing fields fail closed
  with OPENSANDBOX_IDENTITY_INCOMPLETE, nothing is invented locally
  (client_request_id is derived deterministically from invocation_id so a
  retry keeps the same request identity);
- an in-process SandboxInvocation ledger records create_key / execute_key /
  sandbox_id / outcome per invocation_id: replaying the same invocation
  returns the recorded outcome WITHOUT touching the server (remote execute
  count stays 1), and a lost create/execute response is reconciled against
  the server state before any retry;
- create and execute carry DISTINCT idempotency keys, execute timeouts
  reconcile first and never blind-replay; the handler also checks the
  server-side execution list for the execute key before issuing it (worker
  restart / lost response);
- unconfigured: stable ``CAPABILITY_DISABLED`` typed error, exactly like
  the disabled host tools (python/bash/file/stdio MCP stay disabled and
  AC-SEC-12 stays blocked until the real Server acceptance runs).

Server contract: OpenSandbox Server 0.2.2 OpenAPI (PROTOCOL_VERSION);
the image reference pins the 0.2.2 tag. The image DIGEST must be pinned
by the AC-SEC-12 supply-chain acceptance (the registry currently denies
anonymous manifest pulls, so no digest can be honestly claimed here).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
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

CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
IDENTITY_INCOMPLETE = "OPENSANDBOX_IDENTITY_INCOMPLETE"
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

# The durable identity fields the Run worker/BFF MUST supply. Anything
# missing fails closed instead of being invented.
REQUIRED_IDENTITY_FIELDS = (
    "workspace_id",
    "run_id",
    "step_id",
    "attempt_id",
    "invocation_id",
    "client_request_id",
)


@dataclass
class _SandboxInvocationRecord:
    """S3-01: the per-invocation ledger entry (in-process today; the
    P1-RUN-01 durable Run/Attempt/Invocation tables adopt this shape)."""

    invocation_id: str
    create_key: str
    execute_key: str
    sandbox_id: str | None = None
    output: str = ""
    error: str | None = None
    server_state: dict[str, Any] = field(default_factory=dict)
    completed_at: float = 0.0


# Process-wide ledger. A restarted worker cannot read it, which is why the
# handler ALSO reconciles against the SERVER (execution list lookup) before
# issuing any mutation - see _prior_server_execution.
_SANDBOX_LEDGER: dict[str, _SandboxInvocationRecord] = {}


def reset_sandbox_ledger() -> None:
    """Test hook: simulate a worker restart by clearing the process ledger."""
    _SANDBOX_LEDGER.clear()


def _identity_from_request(request: AgentRequest) -> SandboxIdentity | str:
    """S3-01: build the durable identity chain from the BFF runtime payload.

    Every field must already exist in the persisted execution context - a
    missing field returns the name of the missing field (fail-closed); the
    handler turns it into OPENSANDBOX_IDENTITY_INCOMPLETE. The request
    idempotency key (client_request_id) is derived deterministically from
    invocation_id so a retry of the same invocation keeps ONE identity.
    """
    extra = request.extra or {}
    missing = [name for name in REQUIRED_IDENTITY_FIELDS if not extra.get(name)]
    if missing:
        return ", ".join(sorted(missing))
    invocation_id = str(extra["invocation_id"])
    return SandboxIdentity(
        workspace_id=str(extra["workspace_id"]),
        run_id=str(extra["run_id"]),
        step_id=str(extra["step_id"]),
        attempt_id=str(extra["attempt_id"]),
        invocation_id=invocation_id,
        client_request_id=str(extra["client_request_id"]),
    )


def _result_meta(
    *,
    identity: SandboxIdentity,
    limits: SandboxResourceLimits,
    server_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Durable record: identity, policy version, limits and remote state.

    Returned inside ToolResult.data_source, so the BFF persists it with
    the tool evidence (durable Run/Attempt/Invocation bookkeeping).
    """
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

    After a worker restart (empty ledger) or a lost execute response the
    server-side execution list is the reconciliation source of truth: if
    our key is there, the mutation already happened and must NOT be
    re-issued.
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
        # S3-01: never invent identity - fail closed with a typed error.
        return ToolResult(
            success=False,
            name=SANDBOX_TOOL_NAME,
            error=(
                f"{IDENTITY_INCOMPLETE}: the Run worker/BFF must supply the "
                f"complete durable identity; missing: {identity_result}"
            ),
            data_source={
                "source": "opensandbox",
                "error_code": IDENTITY_INCOMPLETE,
            },
        )
    identity = identity_result

    try:
        client = OpenSandboxClient.from_env()
    except OpenSandboxClientError as exc:
        if exc.code == MISSING_CONFIG_ERROR:
            # No host fallback: the capability is simply disabled.
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error=f"{CAPABILITY_DISABLED}: {exc}",
                data_source={"source": "opensandbox", "error_code": exc.code},
            )
        raise

    limits = SandboxResourceLimits()
    create_key = f"create:{identity.invocation_id}"
    execute_key = f"execute:{identity.invocation_id}:1"

    # S3-01: ledger replay - the same invocation replays its recorded
    # outcome without touching the server (remote execute count stays 1).
    record = _SANDBOX_LEDGER.get(identity.invocation_id)
    if record is not None and record.completed_at:
        if record.error:
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error=record.error,
                data_source=_result_meta(
                    identity=identity,
                    limits=limits,
                    server_state=record.server_state,
                ),
            )
        return ToolResult(
            name=SANDBOX_TOOL_NAME,
            content=record.output,
            data_source=_result_meta(
                identity=identity,
                limits=limits,
                server_state=record.server_state,
            ),
        )

    async with client:
        # S3-01: create idempotency - a lost create response is retried with
        # the SAME key; the server dedupes and returns the same sandbox.
        try:
            created = await client.create_sandbox(
                identity, limits, idempotency_key=create_key
            )
        except OpenSandboxClientError as exc:
            if exc.code == UNKNOWN_OUTCOME:
                created = await client.create_sandbox(
                    identity, limits, idempotency_key=create_key
                )
            else:
                raise
        sandbox_id = str(created.get("sandbox_id") or "")
        if not sandbox_id:
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error="OPENSANDBOX_API_ERROR: create response missing sandbox_id",
            )

        # S3-01: reconcile BEFORE mutating - if the server already executed
        # our execute key (worker restart, lost response), take its result
        # instead of re-issuing the command.
        try:
            state = await client.get_sandbox(sandbox_id)
        except OpenSandboxClientError:
            state = {}
        prior = _prior_server_execution(state, execute_key)
        if prior is not None:
            record = _SandboxInvocationRecord(
                invocation_id=identity.invocation_id,
                create_key=create_key,
                execute_key=execute_key,
                sandbox_id=sandbox_id,
                output=str(prior.get("output") or ""),
                server_state=state,
                completed_at=time.time(),
            )
            _SANDBOX_LEDGER[identity.invocation_id] = record
            await _destroy_best_effort(client, sandbox_id)
            return ToolResult(
                name=SANDBOX_TOOL_NAME,
                content=record.output,
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
                # S3-01: never blind-replay a mutation - reconcile the
                # server-side state; if the execute DID land, use its
                # result; otherwise report the unknown outcome verbatim.
                try:
                    state = await client.reconcile(sandbox_id)
                except OpenSandboxClientError as reconcile_exc:
                    return ToolResult(
                        success=False,
                        name=SANDBOX_TOOL_NAME,
                        error=(
                            f"OPENSANDBOX_UNKNOWN_OUTCOME: execute timed out and "
                            f"reconciliation failed: {reconcile_exc}"
                        ),
                        data_source={
                            "source": "opensandbox",
                            "error_code": UNKNOWN_OUTCOME,
                        },
                    )
                prior = _prior_server_execution(state, execute_key)
                if prior is not None:
                    record = _SandboxInvocationRecord(
                        invocation_id=identity.invocation_id,
                        create_key=create_key,
                        execute_key=execute_key,
                        sandbox_id=sandbox_id,
                        output=str(prior.get("output") or ""),
                        server_state=state,
                        completed_at=time.time(),
                    )
                    _SANDBOX_LEDGER[identity.invocation_id] = record
                    await _destroy_best_effort(client, sandbox_id)
                    return ToolResult(
                        name=SANDBOX_TOOL_NAME,
                        content=record.output,
                        data_source=_result_meta(
                            identity=identity, limits=limits, server_state=state
                        ),
                    )
                error = (
                    "OPENSANDBOX_UNKNOWN_OUTCOME: execute timed out; "
                    "server-side state reconciled - no duplicate execution "
                    "was issued"
                )
                record = _SandboxInvocationRecord(
                    invocation_id=identity.invocation_id,
                    create_key=create_key,
                    execute_key=execute_key,
                    sandbox_id=sandbox_id,
                    error=error,
                    server_state=state,
                    completed_at=time.time(),
                )
                _SANDBOX_LEDGER[identity.invocation_id] = record
                return ToolResult(
                    success=False,
                    name=SANDBOX_TOOL_NAME,
                    error=error,
                    data_source=_result_meta(
                        identity=identity, limits=limits, server_state=state
                    ),
                )
            raise

        await _destroy_best_effort(client, sandbox_id)
        output = str(outcome.get("output") or outcome.get("result") or "")
    record = _SandboxInvocationRecord(
        invocation_id=identity.invocation_id,
        create_key=create_key,
        execute_key=execute_key,
        sandbox_id=sandbox_id,
        output=output,
        server_state=outcome,
        completed_at=time.time(),
    )
    _SANDBOX_LEDGER[identity.invocation_id] = record
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
    """The production execution tool (no host fallback).

    Always registered: when the capability is not configured the handler
    returns the stable CAPABILITY_DISABLED typed error instead of touching
    the host.
    """
    return {
        SANDBOX_TOOL_NAME: Tool(
            name=SANDBOX_TOOL_NAME,
            description=SANDBOX_TOOL_DESCRIPTION,
            parameters=SANDBOX_TOOL_PARAMETERS,
            handler=_sandbox_execute_handler,
        )
    }
