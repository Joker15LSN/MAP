"""S4-01: durable SandboxInvocation ledger for OpenSandbox idempotency.

The OpenSandbox tool's exactly-once / isolation boundary is a PostgreSQL
ledger keyed by (workspace_id, invocation_id). This module defines:

- the ledger interface (SandboxInvocationLedger Protocol);
- an in-memory double (InMemorySandboxInvocationLedger) that implements the
  SAME transactional claim semantics, for tests and for running the tool
  where PostgreSQL is unavailable;
- the production implementation (PostgresSandboxInvocationLedger) over raw
  asyncpg against the migration-owned map_control.sandbox_invocations table.

The claim is the atomic boundary: one INSERT under a unique
(workspace_id, invocation_id) primary key. Exactly one caller observes
"owned"; every other caller observes either a terminal replay (same request
digest), a conflict (same identity, different request digest) or an
in-progress row (another caller still owns the invocation). The source of
truth is the ledger row, never an in-process dict and never the (destroyed)
remote sandbox.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol

import asyncpg

# Ledger row statuses.
STATUS_PENDING = "pending"  # claimed; create/execute not yet confirmed
STATUS_CREATED = "created"  # create confirmed; sandbox_id durable
STATUS_SUCCEEDED = "succeeded"  # terminal: execute confirmed, output stored
STATUS_FAILED = "failed"  # terminal: definitive failure recorded
STATUS_UNKNOWN = "unknown"  # terminal: outcome unknown, never replay

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_UNKNOWN}
)

IDEMPOTENCY_CONFLICT = "OPENSANDBOX_IDEMPOTENCY_CONFLICT"
LEDGER_ERROR = "OPENSANDBOX_LEDGER_ERROR"

# The table is created by the BFF's alembic migration in map_control;
# map_core's app role performs DML only.
DEFAULT_SANDBOX_LEDGER_TABLE = "map_control.sandbox_invocations"


class SandboxLedgerError(RuntimeError):
    """Ledger unavailable or violated an invariant (fail closed)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SandboxIdempotencyConflict(SandboxLedgerError):
    """Same (workspace_id, invocation_id) with a different request digest."""

    def __init__(self, message: str) -> None:
        super().__init__(IDEMPOTENCY_CONFLICT, message)


@dataclass(frozen=True)
class SandboxInvocationRecord:
    """One durable invocation fact (the ledger row shape)."""

    workspace_id: str
    invocation_id: str
    request_digest: str
    create_key: str
    execute_key: str
    status: str
    sandbox_id: str | None = None
    output: str | None = None
    error: str | None = None
    server_state: dict[str, Any] | None = None
    created_at: float = 0.0
    completed_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ClaimOutcome:
    """Result of atomically claiming an invocation.

    kind is one of:

    - owned       - this caller inserted the pending row: it MUST drive
      the remote create/execute and settle the row;
    - replay      - a terminal row with the same digest exists: replay it;
    - conflict    - same identity, different digest: never replay;
    - in_progress - another caller owns a non-terminal row: wait for a
      terminal state (never take over, never re-issue a mutation).
    """

    kind: str
    record: SandboxInvocationRecord | None = None


class SandboxInvocationLedger(Protocol):
    async def claim(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        request_digest: str,
        create_key: str,
        execute_key: str,
    ) -> ClaimOutcome: ...

    async def get(
        self, *, workspace_id: str, invocation_id: str
    ) -> SandboxInvocationRecord | None: ...

    async def record_created(
        self, *, workspace_id: str, invocation_id: str, sandbox_id: str
    ) -> None: ...

    async def complete(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        status: str,
        sandbox_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        server_state: dict[str, Any] | None = None,
    ) -> None: ...


def normalize_request_digest(*, command: str, limits: dict[str, Any]) -> str:
    """Stable digest of the execution request (command + resource limits).

    A change to the command or the limits changes the digest, so the SAME
    invocation id with a DIFFERENT payload can never replay an old result.
    """
    payload: dict[str, Any] = {"command": command, "limits": limits}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_create_key(
    *, workspace_id: str, invocation_id: str, request_digest: str
) -> str:
    """Create idempotency key: scoped to workspace + normalized digest."""
    return f"create:{workspace_id}:{invocation_id}:{request_digest}"


def build_execute_key(
    *, workspace_id: str, invocation_id: str, request_digest: str
) -> str:
    """Execute idempotency key: scoped to workspace + normalized digest."""
    return f"execute:{workspace_id}:{invocation_id}:{request_digest}"


class InMemorySandboxInvocationLedger:
    """Test/standalone double with the SAME claim semantics as PostgreSQL.

    claim/get/record/complete never await, so under cooperative asyncio
    scheduling each critical section runs to completion without yielding -
    exactly the atomicity the PostgreSQL unique constraint provides. State
    lives on the instance (like a database), never in a process-global dict.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], SandboxInvocationRecord] = {}

    async def claim(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        request_digest: str,
        create_key: str,
        execute_key: str,
    ) -> ClaimOutcome:
        key = (workspace_id, invocation_id)
        row = self._rows.get(key)
        if row is None:
            record = SandboxInvocationRecord(
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                request_digest=request_digest,
                create_key=create_key,
                execute_key=execute_key,
                status=STATUS_PENDING,
                created_at=time.time(),
            )
            self._rows[key] = record
            return ClaimOutcome("owned", record)
        if row.request_digest != request_digest:
            return ClaimOutcome("conflict", row)
        if row.terminal:
            return ClaimOutcome("replay", row)
        return ClaimOutcome("in_progress", row)

    async def get(
        self, *, workspace_id: str, invocation_id: str
    ) -> SandboxInvocationRecord | None:
        return self._rows.get((workspace_id, invocation_id))

    async def record_created(
        self, *, workspace_id: str, invocation_id: str, sandbox_id: str
    ) -> None:
        row = self._rows.get((workspace_id, invocation_id))
        if row is None:
            raise SandboxLedgerError(
                LEDGER_ERROR, "record_created: invocation row is missing"
            )
        self._rows[(workspace_id, invocation_id)] = replace(
            row, status=STATUS_CREATED, sandbox_id=sandbox_id
        )

    async def complete(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        status: str,
        sandbox_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        server_state: dict[str, Any] | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"complete: {status!r} is not a terminal status"
            )
        row = self._rows.get((workspace_id, invocation_id))
        if row is None:
            raise SandboxLedgerError(
                LEDGER_ERROR, "complete: invocation row is missing"
            )
        self._rows[(workspace_id, invocation_id)] = replace(
            row,
            status=status,
            sandbox_id=sandbox_id if sandbox_id is not None else row.sandbox_id,
            output=output,
            error=error,
            server_state=server_state,
            completed_at=time.time(),
        )

    def clear(self) -> None:
        """Test hook: simulate a brand-new database (no durable rows)."""
        self._rows.clear()


_RECORD_COLUMNS = (
    "workspace_id, invocation_id, request_digest, create_key, execute_key, "
    "status, sandbox_id, output, error, server_state, created_at, completed_at"
)


class PostgresSandboxInvocationLedger:
    """Production ledger over raw asyncpg (map_core has no alembic).

    The table is created by the BFF's alembic migration (map_control schema);
    map_core's app role performs DML only. The (workspace_id, invocation_id)
    PRIMARY KEY is the atomic claim boundary: the INSERT that wins owns the
    invocation; every other INSERT hits a unique violation and observes a
    replay/conflict/in-progress fact.
    """

    def __init__(self, dsn: str, table: str = DEFAULT_SANDBOX_LEDGER_TABLE) -> None:
        if not dsn:
            raise SandboxLedgerError(LEDGER_ERROR, "postgres DSN is required")
        self._dsn = dsn
        self._table = table
        self._pool: asyncpg.Pool | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn, min_size=1, max_size=10
                )
            except asyncpg.PostgresError as exc:
                raise SandboxLedgerError(
                    LEDGER_ERROR, f"postgres pool failed: {exc}"
                ) from exc
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @staticmethod
    def _record_from_row(row: asyncpg.Record) -> SandboxInvocationRecord:
        created_at = row["created_at"]
        completed_at = row["completed_at"]
        return SandboxInvocationRecord(
            workspace_id=row["workspace_id"],
            invocation_id=row["invocation_id"],
            request_digest=row["request_digest"],
            create_key=row["create_key"],
            execute_key=row["execute_key"],
            status=row["status"],
            sandbox_id=row["sandbox_id"],
            output=row["output"],
            error=row["error"],
            server_state=row["server_state"],
            created_at=created_at.timestamp() if created_at is not None else 0.0,
            completed_at=completed_at.timestamp() if completed_at is not None else 0.0,
        )

    async def claim(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        request_digest: str,
        create_key: str,
        execute_key: str,
    ) -> ClaimOutcome:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                inserted = True
                try:
                    await conn.execute(
                        f"INSERT INTO {self._table} "
                        "(workspace_id, invocation_id, request_digest, create_key, "
                        " execute_key, status) VALUES ($1, $2, $3, $4, $5, $6)",
                        workspace_id,
                        invocation_id,
                        request_digest,
                        create_key,
                        execute_key,
                        STATUS_PENDING,
                    )
                except asyncpg.exceptions.UniqueViolationError:
                    inserted = False
                row = await conn.fetchrow(
                    f"SELECT {_RECORD_COLUMNS} FROM {self._table} "
                    "WHERE workspace_id = $1 AND invocation_id = $2",
                    workspace_id,
                    invocation_id,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(LEDGER_ERROR, f"claim failed: {exc}") from exc
        if row is None:  # pragma: no cover - insert succeeded so row exists
            raise SandboxLedgerError(LEDGER_ERROR, "claim: row disappeared")
        record = self._record_from_row(row)
        if inserted:
            return ClaimOutcome("owned", record)
        if record.request_digest != request_digest:
            return ClaimOutcome("conflict", record)
        if record.terminal:
            return ClaimOutcome("replay", record)
        return ClaimOutcome("in_progress", record)

    async def get(
        self, *, workspace_id: str, invocation_id: str
    ) -> SandboxInvocationRecord | None:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {_RECORD_COLUMNS} FROM {self._table} "
                    "WHERE workspace_id = $1 AND invocation_id = $2",
                    workspace_id,
                    invocation_id,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(LEDGER_ERROR, f"get failed: {exc}") from exc
        return self._record_from_row(row) if row is not None else None

    async def record_created(
        self, *, workspace_id: str, invocation_id: str, sandbox_id: str
    ) -> None:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"UPDATE {self._table} SET status = $1, sandbox_id = $2 "
                    "WHERE workspace_id = $3 AND invocation_id = $4 AND status = $5",
                    STATUS_CREATED,
                    sandbox_id,
                    workspace_id,
                    invocation_id,
                    STATUS_PENDING,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"record_created failed: {exc}"
            ) from exc
        if result == "UPDATE 0":
            raise SandboxLedgerError(
                LEDGER_ERROR, "record_created: invocation row missing or not pending"
            )

    async def complete(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        status: str,
        sandbox_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        server_state: dict[str, Any] | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"complete: {status!r} is not a terminal status"
            )
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"UPDATE {self._table} SET status = $1, sandbox_id = $2, "
                    "       output = $3, error = $4, server_state = $5, "
                    "       completed_at = now() "
                    "WHERE workspace_id = $6 AND invocation_id = $7 "
                    "AND status IN ($8, $9)",
                    status,
                    sandbox_id,
                    output,
                    error,
                    server_state,
                    workspace_id,
                    invocation_id,
                    STATUS_PENDING,
                    STATUS_CREATED,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"complete failed: {exc}"
            ) from exc
        if result == "UPDATE 0":
            raise SandboxLedgerError(
                LEDGER_ERROR, "complete: invocation row missing or already terminal"
            )
