"""S5-01: crash-safe durable OpenSandbox execution capability.

The algorithm link reaches the OpenSandbox Server ONLY through this
production tool + OpenSandboxClient; there is NO host fallback. The tool
is registered in the shared tool registry and invoked by both engines
(legacy ToolCallAgent and the AgentScope adapter) through ToolExecutor.

S5-01 hardening (replaces the S4-01 ownerless ledger):

- every claim carries an owner_id + a database-time lease + a non-reusable
  fencing token; a heartbeat renews the lease while the owner drives the
  remote create/execute, and every ledger write (record_created/complete)
  is a CAS bound to that generation;
- a caller that observes a non-terminal row owned by someone else WAITS
  for a terminal fact and, once the lease expires, takes over the row
  atomically and FINISHES the remote flow itself - it never gives up on a
  crashed invocation after a fixed 30s window;
- the terminal write happens BEFORE destroy and is fenced: when the
  terminal state cannot be persisted (ledger failure / lost ownership) the
  caller raises a typed ledger error and MUST NOT destroy the sandbox - the
  remote sandbox stays queryable so the durable reconciler (or a retry of
  the same invocation) can converge the row to a definite terminal state;
- a durable reconciler (started from the Core lifespan) scans expired
  pending/created rows, takes them over and re-drives create/execute with
  the SAME idempotency keys stored in the row - the OpenSandbox server
  deduplicates by key, so re-driving can never double a side effect, and
  when the server state cannot prove what happened the row fails closed to
  unknown (never blind-replayed);
- the row stores the normalized request payload (command + limits +
  identity chain), because after an owner crash it is the only place the
  original command survives for a takeover owner to resume.

Server contract: OpenSandbox Server 0.2.2 OpenAPI (PROTOCOL_VERSION);
the image reference pins the 0.2.2 tag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
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
from .sandbox_ledger import (
    DEFAULT_LEASE_SECONDS,
    IDEMPOTENCY_CONFLICT,
    LEDGER_ERROR,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
    ClaimOutcome,
    InMemorySandboxInvocationLedger,
    PostgresSandboxInvocationLedger,
    SandboxInvocationFence,
    SandboxInvocationLedger,
    SandboxInvocationRecord,
    SandboxLedgerError,
    SandboxOwnershipLost,
    build_create_key,
    build_execute_key,
    normalize_request_digest,
)

logger = logging.getLogger(__name__)

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

# Reconciler cadence.
RECONCILER_INTERVAL_SECONDS = 10.0
RECONCILER_BATCH = 10
RECONCILER_LEASE_SECONDS = 120.0


def _lease_seconds() -> float:
    """Claim lease; tunable via env so crash-recovery tests shrink the window."""
    raw = os.getenv("MAP_SANDBOX_LEASE_SECONDS", "").strip()
    try:
        return float(raw) if raw else DEFAULT_LEASE_SECONDS
    except ValueError:
        return DEFAULT_LEASE_SECONDS


def _in_progress_wait_seconds() -> float:
    raw = os.getenv("MAP_SANDBOX_IN_PROGRESS_WAIT_SECONDS", "").strip()
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0

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


async def close_sandbox_ledger() -> None:
    """S5-01: close the PostgreSQL ledger pool (Core shutdown path).

    Called from the Core lifespan finally block so a stopping process never
    leaks pooled connections to map_control.
    """
    global _ledger
    ledger, _ledger = _ledger, None
    if ledger is not None:
        try:
            await ledger.close()
        except Exception:  # noqa: BLE001 - shutdown best effort
            logger.exception("sandbox ledger close failed during shutdown")


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


def _identity_from_payload(payload: dict[str, Any]) -> SandboxIdentity | None:
    """Rebuild the identity chain stored in a ledger row (reconciler path).

    Returns None when any of the six fields is missing - the reconciler
    then fails the row closed instead of inventing identity.
    """
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        return None
    fields = {name: identity.get(name) for name in REQUIRED_IDENTITY_FIELDS}
    if not all(fields.values()):
        return None
    return SandboxIdentity(**{name: str(fields[name]) for name in REQUIRED_IDENTITY_FIELDS})


def _limits_from_payload(payload: dict[str, Any]) -> SandboxResourceLimits:
    limits = payload.get("limits")
    if isinstance(limits, dict):
        valid = {
            key: int(limits[key])
            for key in SandboxResourceLimits().__dict__
            if key in limits and isinstance(limits[key], int)
        }
        if valid:
            return SandboxResourceLimits(**valid)
    return SandboxResourceLimits()


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


def _owner_id() -> str:
    """A process-unique owner name for claim/heartbeat rows."""
    return f"core-{os.getpid()}-{uuid.uuid4().hex[:8]}"


async def _renew_loop(
    ledger: SandboxInvocationLedger,
    *,
    workspace_id: str,
    invocation_id: str,
    fence: SandboxInvocationFence,
    lease_seconds: float,
    ownership_lost: asyncio.Event,
) -> None:
    """Heartbeat the lease until the terminal write or ownership loss."""
    interval = max(0.2, lease_seconds / 3.0)
    while not ownership_lost.is_set():
        await asyncio.sleep(interval)
        try:
            ok = await ledger.renew(
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                fence=fence,
                lease_seconds=lease_seconds,
            )
        except SandboxLedgerError:
            ok = False
        if not ok:
            ownership_lost.set()
            return


async def _wait_and_takeover(
    ledger: SandboxInvocationLedger,
    *,
    workspace_id: str,
    invocation_id: str,
    owner_id: str,
    timeout_s: float | None = None,
) -> ClaimOutcome | None:
    """Resolve an in-progress invocation without ever stealing a live lease.

    While the owning generation holds a LIVE lease we only poll for a
    terminal fact. Once the lease is expired we attempt the atomic takeover
    CAS; winning it makes THIS caller the owner that finishes the remote
    flow (the crashed owner's work is resumed, never replayed blindly).
    Returns None when the budget expires while another generation still
    holds the row.
    """
    if timeout_s is None:
        timeout_s = _in_progress_wait_seconds()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = await ledger.get(
            workspace_id=workspace_id, invocation_id=invocation_id
        )
        if record is None:
            return None  # row vanished; the caller re-claims from scratch
        if record.terminal:
            return ClaimOutcome("replay", record)
        if record.lease_expires_at <= 0.0 or record.lease_expires_at <= time.time():
            outcome = await ledger.takeover(
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                owner_id=owner_id,
                lease_seconds=_lease_seconds(),
            )
            if outcome.kind in ("takeover", "replay"):
                return outcome
        await asyncio.sleep(0.05)
    return None


async def _settle_fenced(
    ledger: SandboxInvocationLedger,
    *,
    identity: SandboxIdentity,
    fence: SandboxInvocationFence,
    status: str,
    sandbox_id: str | None = None,
    output: str | None = None,
    error: str | None = None,
    server_state: dict[str, Any] | None = None,
) -> None:
    """Persist the terminal state under OUR fence; NEVER destroy on failure.

    A ledger failure here leaves the row non-terminal (pending/created) for
    the durable reconciler. The caller MUST NOT destroy the sandbox in that
    case - the sandbox is the only place the remote outcome can still be
    queried - and MUST NOT report success.
    """
    try:
        await ledger.complete(
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            status=status,
            fence=fence,
            sandbox_id=sandbox_id,
            output=output,
            error=error,
            server_state=server_state,
        )
    except SandboxLedgerError as exc:
        raise SandboxLedgerError(
            LEDGER_ERROR,
            f"terminal state for invocation {identity.invocation_id!r} could "
            f"not be persisted ({exc}); the remote sandbox was NOT destroyed "
            "so the durable reconciler can converge the invocation",
        ) from exc


def _settle_failed_result(identity: SandboxIdentity, exc: Exception) -> ToolResult:
    return ToolResult(
        success=False,
        name=SANDBOX_TOOL_NAME,
        error=str(exc),
        data_source={"source": "opensandbox", "error_code": LEDGER_ERROR},
    )


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
    # S5-01: the row must survive an owner crash, so it stores the full
    # normalized request (command + limits + identity chain).
    request_payload: dict[str, Any] = {
        "command": command,
        "limits": limits.to_dict(),
        "identity": identity.to_dict(),
    }

    owner_id = _owner_id()
    ledger = _get_sandbox_ledger()
    try:
        claim = await ledger.claim(
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            request_digest=request_digest,
            create_key=create_key,
            execute_key=execute_key,
            request_payload=request_payload,
            owner_id=owner_id,
            lease_seconds=_lease_seconds(),
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
        # S5-01: wait for a terminal fact; when the owner's lease expires,
        # take the row over and finish the remote flow ourselves instead of
        # giving up after 30s (owner-crash convergence).
        resolved = await _wait_and_takeover(
            ledger,
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            owner_id=owner_id,
        )
        if resolved is None:
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
        claim = resolved
    if claim.kind == "replay":
        return _replay(claim.record, identity, limits)  # type: ignore[arg-type]

    # owned / takeover: this caller drives (or resumes) the remote flow.
    if claim.fence is None or claim.record is None:  # pragma: no cover
        raise RuntimeError("owned claim without fence/record")
    fence = claim.fence

    try:
        client = OpenSandboxClient.from_env()
    except OpenSandboxClientError as exc:
        if exc.code == MISSING_CONFIG_ERROR:
            # No host fallback: the capability is simply disabled.
            try:
                await _settle_fenced(
                    ledger,
                    identity=identity,
                    fence=fence,
                    status=STATUS_FAILED,
                    error=f"{CAPABILITY_DISABLED}: {exc}",
                )
            except SandboxLedgerError as ledger_exc:
                return _settle_failed_result(identity, ledger_exc)
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error=f"{CAPABILITY_DISABLED}: {exc}",
                data_source={"source": "opensandbox", "error_code": exc.code},
            )
        raise

    ownership_lost = asyncio.Event()
    renew_task = asyncio.create_task(
        _renew_loop(
            ledger,
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            fence=fence,
            lease_seconds=_lease_seconds(),
            ownership_lost=ownership_lost,
        )
    )
    try:
        return await _drive_remote_execution(
            command=command,
            identity=identity,
            limits=limits,
            create_key=create_key,
            execute_key=execute_key,
            ledger=ledger,
            record=claim.record,
            fence=fence,
            client=client,
            ownership_lost=ownership_lost,
        )
    except OpenSandboxClientError as exc:
        # Definitive remote failure: settle failed under our fence so a
        # retry replays the same terminal fact, then propagate. If the
        # fenced settle itself fails the row stays for the reconciler.
        try:
            await _settle_fenced(
                ledger,
                identity=identity,
                fence=fence,
                status=STATUS_FAILED,
                error=str(exc),
            )
        except SandboxLedgerError:
            logger.exception("failed terminal settle after remote error")
        raise
    except SandboxLedgerError:
        # Ownership lost / terminal write failure: the drive function has
        # NOT destroyed the sandbox. Surface the typed error to the caller;
        # the reconciler converges the row.
        raise
    except Exception as exc:  # noqa: BLE001 - handler boundary
        try:
            await _settle_fenced(
                ledger,
                identity=identity,
                fence=fence,
                status=STATUS_FAILED,
                error=str(exc),
            )
        except SandboxLedgerError:
            logger.exception("failed terminal settle after unexpected error")
        raise
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew_task
        await client.aclose()


async def _drive_remote_execution(
    *,
    command: str,
    identity: SandboxIdentity,
    limits: SandboxResourceLimits,
    create_key: str,
    execute_key: str,
    ledger: SandboxInvocationLedger,
    record: SandboxInvocationRecord,
    fence: SandboxInvocationFence,
    client: OpenSandboxClient,
    ownership_lost: asyncio.Event,
) -> ToolResult:
    """The ONE caller that owns the fence performs create/execute/destroy.

    Resume contract (S5-01): a takeover owner starts from the row it won -
    a pending row re-issues create with the SAME idempotency key (the
    server deduplicates, so this can never create a second sandbox); a
    created row reuses the recorded sandbox_id and reconciles the server
    state before ever re-issuing execute.

    Destroy contract: the sandbox is destroyed ONLY after a fenced terminal
    write succeeded. Any ledger failure here raises WITHOUT destroying the
    sandbox, so the remote outcome stays queryable for the reconciler.
    """

    def _ownership_ok() -> None:
        if ownership_lost.is_set():
            raise SandboxOwnershipLost(
                "sandbox lease heartbeat lost ownership mid-flight; "
                "refusing to write terminal state or destroy the sandbox"
            )

    sandbox_id: str | None = None
    resumed = record.status == STATUS_CREATED and bool(record.sandbox_id)
    if resumed:
        # Takeover resume: the durable sandbox_id survives the crashed owner.
        sandbox_id = record.sandbox_id
    else:
        # Create. A lost create response is NOT blind-retried: the
        # idempotency key makes a re-send safe (server dedup), and the
        # takeover owner re-sends the SAME key from the ledger row.
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
                await _settle_fenced(
                    ledger,
                    identity=identity,
                    fence=fence,
                    status=STATUS_UNKNOWN,
                    error=error,
                )
                return ToolResult(
                    success=False,
                    name=SANDBOX_TOOL_NAME,
                    error=error,
                    data_source={"source": "opensandbox", "error_code": UNKNOWN_OUTCOME},
                )
            raise
        _ownership_ok()
        sandbox_id = str(created.get("sandbox_id") or "")
        if not sandbox_id:
            error = "OPENSANDBOX_API_ERROR: create response missing sandbox_id"
            await _settle_fenced(
                ledger,
                identity=identity,
                fence=fence,
                status=STATUS_FAILED,
                error=error,
            )
            return ToolResult(
                success=False, name=SANDBOX_TOOL_NAME, error=error
            )
        # Fenced durable write BEFORE any further mutation; on failure the
        # sandbox is left alive for the reconciler (never destroyed here).
        await ledger.record_created(
            workspace_id=identity.workspace_id,
            invocation_id=identity.invocation_id,
            sandbox_id=sandbox_id,
            fence=fence,
        )
        record = replace_status(record, status="created", sandbox_id=sandbox_id)

    # Reconcile BEFORE mutating (sandbox still exists): if the server
    # already executed our key, take its result instead of re-issuing.
    try:
        state = await client.get_sandbox(sandbox_id)
    except OpenSandboxClientError as exc:
        if resumed:
            # S5-01 fail-closed: on a RESUMED (created) row the prior owner
            # may already have executed; an unqueryable sandbox cannot prove
            # the absence of that execution, so execute is never re-issued.
            error = (
                "OPENSANDBOX_UNKNOWN_OUTCOME: the sandbox is no longer "
                "queryable and the prior execution cannot be proven "
                "absent; refusing to re-issue the mutation"
            )
            await _settle_fenced(
                ledger,
                identity=identity,
                fence=fence,
                status=STATUS_UNKNOWN,
                sandbox_id=sandbox_id,
                error=error,
            )
            return ToolResult(
                success=False,
                name=SANDBOX_TOOL_NAME,
                error=error,
                data_source={"source": "opensandbox", "error_code": UNKNOWN_OUTCOME},
            )
        # Fresh create in THIS call: no prior owner ever executed (the
        # protocol records the sandbox before any execute), so a transient
        # GET failure is not a safety hazard - execute is still keyed.
        state = {}
    prior = _prior_server_execution(state, execute_key)
    if prior is not None:
        output = str(prior.get("output") or "")
        await _settle_fenced(
            ledger,
            identity=identity,
            fence=fence,
            status=STATUS_SUCCEEDED,
            sandbox_id=sandbox_id,
            output=output,
            server_state=state,
        )
        _ownership_ok()
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
                await _settle_fenced(
                    ledger,
                    identity=identity,
                    fence=fence,
                    status=STATUS_UNKNOWN,
                    sandbox_id=sandbox_id,
                    error=error,
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
                await _settle_fenced(
                    ledger,
                    identity=identity,
                    fence=fence,
                    status=STATUS_SUCCEEDED,
                    sandbox_id=sandbox_id,
                    output=output,
                    server_state=state,
                )
                _ownership_ok()
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
            await _settle_fenced(
                ledger,
                identity=identity,
                fence=fence,
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

    # Record the terminal success BEFORE destroy: the ledger is the durable
    # source of truth once the sandbox is gone. A failed terminal write
    # raises HERE, before destroy, leaving the sandbox queryable.
    await _settle_fenced(
        ledger,
        identity=identity,
        fence=fence,
        status=STATUS_SUCCEEDED,
        sandbox_id=sandbox_id,
        output=output,
        server_state=outcome,
    )
    _ownership_ok()
    await _destroy_best_effort(client, sandbox_id)
    return ToolResult(
        name=SANDBOX_TOOL_NAME,
        content=output,
        data_source=_result_meta(
            identity=identity, limits=limits, server_state=outcome
        ),
    )


def replace_status(
    record: SandboxInvocationRecord, *, status: str, sandbox_id: str
) -> SandboxInvocationRecord:
    """Local mirror update after a successful fenced record_created."""
    from dataclasses import replace

    return replace(record, status=status, sandbox_id=sandbox_id)


async def _destroy_best_effort(client: OpenSandboxClient, sandbox_id: str) -> None:
    # Best-effort teardown; the server also enforces sandbox TTL. Called
    # ONLY after a fenced terminal write succeeded.
    try:
        await client.destroy_sandbox(sandbox_id)
    except OpenSandboxClientError:
        pass


class SandboxReconciler:
    """Durable reconciler for crashed non-terminal invocations (S5-01).

    Scans expired pending/created rows, atomically takes each over and
    re-drives the remote create/execute with the SAME idempotency keys the
    crashed owner used (stored in the row). The OpenSandbox server dedupes
    by key, so re-driving never doubles a side effect; when the remote
    state cannot prove what happened, the row fails closed to unknown.

    The reconciler runs in EVERY Core process; the takeover CAS guarantees
    exactly one process drives each row. Rows whose lease is still alive
    are never touched.
    """

    def __init__(
        self,
        ledger: SandboxInvocationLedger,
        *,
        interval_s: float = RECONCILER_INTERVAL_SECONDS,
        batch: int = RECONCILER_BATCH,
        lease_seconds: float = RECONCILER_LEASE_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._interval_s = interval_s
        self._batch = batch
        self._lease_seconds = lease_seconds
        self._owner_id = f"reconciler-{os.getpid()}"

    async def reconcile_once(self) -> int:
        """Scan and converge expired rows once; returns the count driven."""
        try:
            client = OpenSandboxClient.from_env()
        except OpenSandboxClientError:
            return 0  # capability not configured: nothing to converge
        rows = await self._ledger.list_expired(limit=self._batch)
        if not rows:
            await client.aclose()
            return 0
        reconciled = 0
        try:
            for row in rows:
                try:
                    if await self._reconcile_row(row, client):
                        reconciled += 1
                except Exception:  # noqa: BLE001 - one row never blocks the scan
                    logger.exception(
                        "sandbox reconciler failed to converge %s/%s",
                        row.workspace_id,
                        row.invocation_id,
                    )
        finally:
            await client.aclose()
        return reconciled

    async def _reconcile_row(self, row: SandboxInvocationRecord, client: OpenSandboxClient) -> bool:
        outcome = await self._ledger.takeover(
            workspace_id=row.workspace_id,
            invocation_id=row.invocation_id,
            owner_id=self._owner_id,
            lease_seconds=self._lease_seconds,
        )
        if outcome.kind != "takeover" or outcome.fence is None or outcome.record is None:
            return False  # replay / still alive / gone: nothing to drive

        payload = outcome.record.request_payload or {}
        command = str(payload.get("command") or "").strip()
        identity = _identity_from_payload(payload)
        if not command or identity is None:
            # The row cannot be resumed without the original request
            # (pre-S5-01 rows). Never invent a command: fail closed.
            error = (
                "OPENSANDBOX_UNKNOWN_OUTCOME: the invocation row does not "
                "carry the original request payload; the remote outcome "
                "cannot be proven and will not be blindly replayed"
            )
            try:
                await _settle_fenced(
                    self._ledger,
                    identity=identity or _placeholder_identity(row),
                    fence=outcome.fence,
                    status=STATUS_UNKNOWN,
                    sandbox_id=outcome.record.sandbox_id,
                    error=error,
                )
            except SandboxLedgerError:
                logger.exception("reconciler could not settle payload-less row")
            return True

        limits = _limits_from_payload(payload)
        ownership_lost = asyncio.Event()
        renew_task = asyncio.create_task(
            _renew_loop(
                self._ledger,
                workspace_id=row.workspace_id,
                invocation_id=row.invocation_id,
                fence=outcome.fence,
                lease_seconds=self._lease_seconds,
                ownership_lost=ownership_lost,
            )
        )
        try:
            await _drive_remote_execution(
                command=command,
                identity=identity,
                limits=limits,
                create_key=outcome.record.create_key,
                execute_key=outcome.record.execute_key,
                ledger=self._ledger,
                record=outcome.record,
                fence=outcome.fence,
                client=client,
                ownership_lost=ownership_lost,
            )
            return True
        except OpenSandboxClientError as exc:
            # Definitive remote failure: settle failed under our fence.
            try:
                await _settle_fenced(
                    self._ledger,
                    identity=identity,
                    fence=outcome.fence,
                    status=STATUS_FAILED,
                    sandbox_id=outcome.record.sandbox_id,
                    error=str(exc),
                )
            except SandboxLedgerError:
                logger.exception("reconciler failed settle after remote error")
            return True
        except SandboxLedgerError as exc:
            logger.warning(
                "sandbox reconciler could not settle %s/%s: %s",
                row.workspace_id,
                row.invocation_id,
                exc,
            )
            return False
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            try:
                await self.reconcile_once()
            except SandboxLedgerError as exc:
                logger.warning("sandbox reconciler scan failed: %s", exc)
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.exception("sandbox reconciler iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


def _placeholder_identity(row: SandboxInvocationRecord) -> SandboxIdentity:
    """Identity used ONLY to settle payload-less pre-S5-01 rows unknown."""
    return SandboxIdentity(
        workspace_id=row.workspace_id,
        run_id="unknown",
        step_id="unknown",
        attempt_id="unknown",
        invocation_id=row.invocation_id,
        client_request_id="unknown",
    )


def create_sandbox_reconciler() -> SandboxReconciler | None:
    """Build the durable reconciler for the production PG ledger.

    Returns None when PostgreSQL is not configured or when a test injected
    an in-memory ledger (reconciliation there is driven by the claim
    takeover path instead).
    """
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    if not dsn:
        return None
    ledger = _get_sandbox_ledger()
    if isinstance(ledger, InMemorySandboxInvocationLedger):
        return None
    return SandboxReconciler(ledger)


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
