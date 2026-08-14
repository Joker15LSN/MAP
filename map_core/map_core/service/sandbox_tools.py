"""S2-06: production wiring of the OpenSandbox execution capability.

The algorithm link reaches the OpenSandbox Server ONLY through this
production tool + :class:`OpenSandboxClient`; there is NO host fallback.
The tool is registered in the shared tool registry by
:class:`AgentDispatcher` (``_register_dynamic_tools``), so both engines
(legacy ToolCallAgent and the AgentScope adapter) invoke the same handler.

Behavior:

- configured (MAP_OPENSANDBOX_URL + MAP_OPENSANDBOX_API_KEY): every call
  carries the durable identity chain (workspace/run/step/attempt/
  invocation/client_request_id) plus the request idempotency key; the
  sandbox identity, resource limits, protocol version and the server-side
  state are returned in the result (persisted by the BFF via the
  tool_evidence channel);
- execute timeouts NEVER blind-replay: the client reconciles the remote
  sandbox state first and reports the server-side outcome;
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
import uuid
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


def _identity_from_request(request: AgentRequest) -> SandboxIdentity:
    """Build the durable identity chain from the BFF runtime payload.

    Missing identifiers are generated locally so the chain is always
    complete; the BFF persists the mapping between its durable ids and
    these values via the tool_evidence channel.
    """
    extra = request.extra or {}

    def pick(key: str) -> str:
        value = extra.get(key)
        return str(value) if value else str(uuid.uuid4())

    return SandboxIdentity(
        workspace_id=pick("workspace_id"),
        run_id=pick("run_id"),
        step_id=pick("step_id"),
        attempt_id=pick("attempt_id"),
        invocation_id=pick("invocation_id"),
        client_request_id=pick("client_request_id"),
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

    identity = _identity_from_request(request)
    limits = SandboxResourceLimits()

    async with client:
        created = await client.create_sandbox(identity, limits)
        sandbox_id = str(created.get("sandbox_id") or "")
        if not sandbox_id:
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error="OPENSANDBOX_API_ERROR: create response missing sandbox_id",
            )
        try:
            outcome = await client.execute(
                sandbox_id, identity, command, limits.timeout_seconds
            )
        except OpenSandboxClientError as exc:
            if exc.code == UNKNOWN_OUTCOME:
                # S2-06: never blind-replay a mutation - reconcile the
                # server-side state first and report it verbatim.
                try:
                    state = await client.reconcile(sandbox_id)
                except OpenSandboxClientError as reconcile_exc:
                    return ToolResult(
                        success=False,
                        name=SANDBOX_TOOL_NAME,
                        error=(
                            f"OPENSANDBOX_UNKNOWN_OUTCOME: execute timed out and "
                            f"reconciliation failed: {reconcile_exc.message}"
                        ),
                        data_source={
                            "source": "opensandbox",
                            "error_code": UNKNOWN_OUTCOME,
                        },
                    )
                return ToolResult(
                    success=False,
                    name=SANDBOX_TOOL_NAME,
                    error=(
                        "OPENSANDBOX_UNKNOWN_OUTCOME: execute timed out; "
                        "server-side state reconciled - no duplicate execution "
                        "was issued"
                    ),
                    data_source=_result_meta(
                        identity=identity, limits=limits, server_state=state
                    ),
                )
            raise
        finally:
            # Best-effort teardown; the server also enforces sandbox TTL.
            try:
                await client.destroy_sandbox(sandbox_id)
            except OpenSandboxClientError:
                pass

    return ToolResult(
        name=SANDBOX_TOOL_NAME,
        content=str(outcome.get("output") or outcome.get("result") or ""),
        data_source=_result_meta(
            identity=identity, limits=limits, server_state=outcome
        ),
    )


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
