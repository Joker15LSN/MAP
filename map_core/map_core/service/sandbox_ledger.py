"""S5-01: crash-safe durable SandboxInvocation ledger for OpenSandbox.

S4-01 made the (workspace_id, invocation_id) claim atomic, but non-terminal
rows (pending/created) had no owner, lease or fencing token: a process crash
between the remote create/execute and the ledger write left the row
permanently non-terminal and every later caller could only wait.

This module adds the ownership fence (R5-P1-SANDBOX):

- every claim INSERT records owner_id + a non-reusable fencing_token +
  attempt + a database-time lease_expires_at;
- every owner-sensitive write (record_created / complete) is a CAS UPDATE
  bound to the caller's OWN generation (token + owner + attempt), so a
  superseded owner observes rowcount 0 instead of overwriting the
  generation that took over;
- a non-terminal row whose lease expired is taken over by ONE atomic CAS
  UPDATE that mints a NEW token and bumps the attempt - exactly one caller
  wins; the loser re-reads the facts;
- renew heartbeats the lease under the caller's own generation;
- list_expired / takeover give the durable reconciler the scan and the
  takeover primitives it needs to converge crashed invocations;
- rows written before this migration carry NULL fence columns: NULL matches
  IS NOT DISTINCT FROM (a valid observed generation) and a NULL lease
  counts as ALREADY EXPIRED, so pre-migration rows stay takeover-able.

The request payload (command + resource limits) is part of the row, because
after an owner crash it is the ONLY place the original command survives for
a takeover owner / reconciler to re-drive with the SAME idempotency keys.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
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
OWNERSHIP_LOST = "OPENSANDBOX_OWNERSHIP_LOST"

# The table is created by the BFF's alembic migration in map_control;
# map_core's app role performs DML only.
DEFAULT_SANDBOX_LEDGER_TABLE = "map_control.sandbox_invocations"

# How long one claim may own the row before the lease can be taken over.
DEFAULT_LEASE_SECONDS = 60.0


class SandboxLedgerError(RuntimeError):
    """Ledger unavailable or violated an invariant (fail closed)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SandboxIdempotencyConflict(SandboxLedgerError):
    """Same (workspace_id, invocation_id) with a different request digest."""

    def __init__(self, message: str) -> None:
        super().__init__(IDEMPOTENCY_CONFLICT, message)


class SandboxOwnershipLost(SandboxLedgerError):
    """A fenced write matched 0 rows: the generation was superseded or the
    row already reached a terminal state. The caller MUST NOT claim the
    outcome, MUST NOT destroy the remote sandbox, and must re-resolve."""

    def __init__(self, message: str) -> None:
        super().__init__(OWNERSHIP_LOST, message)


@dataclass(frozen=True)
class SandboxInvocationFence:
    """The exclusive generation credential for one owner.

    token is minted per generation (fresh claim OR takeover) and is never
    reused; attempt increments on every takeover. Every owner-sensitive
    ledger write carries all three values as its CAS predicate.
    """

    token: str
    owner_id: str
    attempt: int


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
    request_payload: dict[str, Any] | None = None
    owner_id: str | None = None
    lease_expires_at: float = 0.0
    fencing_token: str | None = None
    attempt: int = 0
    created_at: float = 0.0
    completed_at: float = 0.0
    updated_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ClaimOutcome:
    """Result of atomically claiming an invocation.

    kind is one of:

    - owned       - this caller inserted the pending row (or won the
      takeover CAS on an expired row): it MUST drive the remote
      create/execute and settle the row; fence is the exclusive credential
      for every later write;
    - takeover    - same as owned, but the row previously belonged to a
      crashed/superseded generation; record.status may already be created
      with a durable sandbox_id to resume from;
    - replay      - a terminal row with the same digest exists: replay it;
    - conflict    - same identity, different digest: never replay;
    - in_progress - another caller owns a NON-terminal row whose lease is
      still alive: never take over, never re-issue a mutation (the caller
      may retry the takeover once the lease expires).
    """

    kind: str
    record: SandboxInvocationRecord | None = None
    fence: SandboxInvocationFence | None = None


class SandboxInvocationLedger(Protocol):
    async def claim(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        request_digest: str,
        create_key: str,
        execute_key: str,
        request_payload: dict[str, Any],
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> ClaimOutcome: ...

    async def get(
        self, *, workspace_id: str, invocation_id: str
    ) -> SandboxInvocationRecord | None: ...

    async def renew(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        fence: SandboxInvocationFence,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool: ...

    async def record_created(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        sandbox_id: str,
        fence: SandboxInvocationFence,
    ) -> None: ...

    async def complete(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        status: str,
        fence: SandboxInvocationFence,
        sandbox_id: str | None = None,
        output: str | None = None,
        error: str | None = None,
        server_state: dict[str, Any] | None = None,
    ) -> None: ...

    async def takeover(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> ClaimOutcome: ...

    async def list_expired(
        self, *, limit: int = 10
    ) -> list[SandboxInvocationRecord]: ...

    async def close(self) -> None: ...


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


def new_fence(owner_id: str, attempt: int = 0) -> SandboxInvocationFence:
    """Mint a non-reusable generation credential."""
    return SandboxInvocationFence(
        token=uuid.uuid4().hex, owner_id=owner_id, attempt=attempt
    )


class InMemorySandboxInvocationLedger:
    """Test/standalone double with the SAME claim/takeover semantics as PG.

    claim/get/record/complete never await, so under cooperative asyncio
    scheduling each critical section runs to completion without yielding -
    exactly the atomicity the PostgreSQL unique constraint + CAS UPDATE
    provides. Leases use wall-clock time.time() (the database clock for
    PostgreSQL); NULL lease / NULL fence semantics mirror the pre-migration
    rows so takeover behavior is exercised by tests too.
    """

    def __init__(self, *, clock: Any = None) -> None:
        self._rows: dict[tuple[str, str], SandboxInvocationRecord] = {}
        self._clock = clock or time.time
        # Test hooks (S5-01 window E): fail the NEXT complete/record_created
        # call once to simulate a ledger outage at the worst moment.
        self.fail_complete_next = False
        self.fail_record_created_next = False

    def _now(self) -> float:
        return float(self._clock())

    def _expired(self, row: SandboxInvocationRecord) -> bool:
        return row.lease_expires_at <= 0.0 or row.lease_expires_at <= self._now()

    async def claim(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        request_digest: str,
        create_key: str,
        execute_key: str,
        request_payload: dict[str, Any],
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> ClaimOutcome:
        key = (workspace_id, invocation_id)
        row = self._rows.get(key)
        if row is None:
            fence = new_fence(owner_id)
            now = self._now()
            record = SandboxInvocationRecord(
                workspace_id=workspace_id,
                invocation_id=invocation_id,
                request_digest=request_digest,
                create_key=create_key,
                execute_key=execute_key,
                request_payload=request_payload,
                status=STATUS_PENDING,
                owner_id=owner_id,
                lease_expires_at=now + float(lease_seconds),
                fencing_token=fence.token,
                attempt=0,
                created_at=now,
                updated_at=now,
            )
            self._rows[key] = record
            return ClaimOutcome("owned", record, fence)
        if row.request_digest != request_digest:
            return ClaimOutcome("conflict", row)
        if row.terminal:
            return ClaimOutcome("replay", row)
        if not self._expired(row):
            return ClaimOutcome("in_progress", row)
        return await self.takeover(
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            owner_id=owner_id,
            lease_seconds=lease_seconds,
        )

    async def get(
        self, *, workspace_id: str, invocation_id: str
    ) -> SandboxInvocationRecord | None:
        return self._rows.get((workspace_id, invocation_id))

    async def renew(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        fence: SandboxInvocationFence,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        row = self._rows.get((workspace_id, invocation_id))
        if row is None or row.terminal:
            return False
        if (
            row.fencing_token != fence.token
            or row.owner_id != fence.owner_id
            or row.attempt != fence.attempt
        ):
            return False
        now = self._now()
        self._rows[(workspace_id, invocation_id)] = replace(
            row,
            lease_expires_at=now + float(lease_seconds),
            updated_at=now,
        )
        return True

    async def record_created(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        sandbox_id: str,
        fence: SandboxInvocationFence,
    ) -> None:
        row = self._rows.get((workspace_id, invocation_id))
        if (
            row is None
            or row.status != STATUS_PENDING
            or row.fencing_token != fence.token
            or row.owner_id != fence.owner_id
            or row.attempt != fence.attempt
        ):
            raise SandboxOwnershipLost(
                "record_created: ownership lost or row not pending"
            )
        if self.fail_record_created_next:
            self.fail_record_created_next = False
            raise SandboxLedgerError(
                LEDGER_ERROR, "record_created: simulated ledger failure"
            )
        self._rows[(workspace_id, invocation_id)] = replace(
            row,
            status=STATUS_CREATED,
            sandbox_id=sandbox_id,
            updated_at=self._now(),
        )

    async def complete(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        status: str,
        fence: SandboxInvocationFence,
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
        if (
            row is None
            or row.status not in (STATUS_PENDING, STATUS_CREATED)
            or row.fencing_token != fence.token
            or row.owner_id != fence.owner_id
            or row.attempt != fence.attempt
        ):
            raise SandboxOwnershipLost(
                "complete: ownership lost or row already terminal"
            )
        if self.fail_complete_next:
            self.fail_complete_next = False
            raise SandboxLedgerError(
                LEDGER_ERROR, "complete: simulated ledger failure"
            )
        now = self._now()
        self._rows[(workspace_id, invocation_id)] = replace(
            row,
            status=status,
            sandbox_id=sandbox_id if sandbox_id is not None else row.sandbox_id,
            output=output,
            error=error,
            server_state=server_state,
            completed_at=now,
            lease_expires_at=0.0,
            updated_at=now,
        )

    async def takeover(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> ClaimOutcome:
        key = (workspace_id, invocation_id)
        row = self._rows.get(key)
        if row is None:
            return ClaimOutcome("gone")
        if row.terminal:
            return ClaimOutcome("replay", row)
        if not self._expired(row):
            return ClaimOutcome("in_progress", row)
        fence = new_fence(owner_id, attempt=row.attempt + 1)
        now = self._now()
        taken = replace(
            row,
            owner_id=owner_id,
            lease_expires_at=now + float(lease_seconds),
            fencing_token=fence.token,
            attempt=fence.attempt,
            updated_at=now,
        )
        self._rows[key] = taken
        return ClaimOutcome("takeover", taken, fence)

    async def list_expired(self, *, limit: int = 10) -> list[SandboxInvocationRecord]:
        expired = [
            row
            for row in self._rows.values()
            if not row.terminal and self._expired(row)
        ]
        expired.sort(key=lambda row: row.updated_at)
        return expired[:limit]

    async def close(self) -> None:
        pass

    def clear(self) -> None:
        """Test hook: simulate a brand-new database (no durable rows)."""
        self._rows.clear()


_RECORD_COLUMNS = (
    "workspace_id, invocation_id, request_digest, create_key, execute_key, "
    "status, sandbox_id, output, error, server_state, request_payload, "
    "owner_id, lease_expires_at, fencing_token, attempt, created_at, "
    "completed_at, updated_at"
)

_TAKEOVER_ATTEMPTS = 5


class PostgresSandboxInvocationLedger:
    """Production ledger over raw asyncpg (map_core has no alembic).

    The table is created by the BFF's alembic migration (map_control
    schema); map_core's app role performs DML only. The
    (workspace_id, invocation_id) PRIMARY KEY is the atomic claim boundary:
    the INSERT that wins owns the invocation; every other INSERT hits a
    unique violation and observes a replay/conflict/in-progress fact.

    Fencing (R5-P1-SANDBOX): each claim mints a fresh token + attempt and a
    database-time lease. record_created/complete/renew are CAS UPDATEs bound
    to the caller's generation; a takeover of an EXPIRED non-terminal row is
    ONE atomic UPDATE (new token, attempt bump) whose rowcount decides the
    single winner.
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
        updated_at = row["updated_at"]
        lease_expires_at = row["lease_expires_at"]
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
            request_payload=row["request_payload"],
            owner_id=row["owner_id"],
            lease_expires_at=(
                lease_expires_at.timestamp() if lease_expires_at is not None else 0.0
            ),
            fencing_token=row["fencing_token"],
            attempt=int(row["attempt"] or 0),
            created_at=created_at.timestamp() if created_at is not None else 0.0,
            completed_at=completed_at.timestamp() if completed_at is not None else 0.0,
            updated_at=updated_at.timestamp() if updated_at is not None else 0.0,
        )

    async def claim(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        request_digest: str,
        create_key: str,
        execute_key: str,
        request_payload: dict[str, Any],
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> ClaimOutcome:
        fence = new_fence(owner_id)
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                inserted = True
                try:
                    await conn.execute(
                        f"INSERT INTO {self._table} "
                        "(workspace_id, invocation_id, request_digest, create_key, "
                        " execute_key, request_payload, status, owner_id, "
                        " fencing_token, attempt, lease_expires_at, updated_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, "
                        "        now() + make_interval(secs => $10), now())",
                        workspace_id,
                        invocation_id,
                        request_digest,
                        create_key,
                        execute_key,
                        json.dumps(request_payload, sort_keys=True),
                        STATUS_PENDING,
                        owner_id,
                        fence.token,
                        float(lease_seconds),
                    )
                except asyncpg.exceptions.UniqueViolationError:
                    inserted = False
                row = await conn.fetchrow(
                    f"SELECT {_RECORD_COLUMNS} FROM {self._table} "
                    "WHERE workspace_id = $1 AND invocation_id = $2",
                    workspace_id,
                    invocation_id,
                )
                if row is None:  # pragma: no cover - insert succeeded
                    raise SandboxLedgerError(LEDGER_ERROR, "claim: row disappeared")
                record = self._record_from_row(row)
                if inserted:
                    return ClaimOutcome("owned", record, fence)
                if record.request_digest != request_digest:
                    return ClaimOutcome("conflict", record)
                if record.terminal:
                    return ClaimOutcome("replay", record)
                return await self._takeover_loop(
                    conn,
                    workspace_id,
                    invocation_id,
                    owner_id,
                    lease_seconds,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(LEDGER_ERROR, f"claim failed: {exc}") from exc

    async def _takeover_loop(
        self,
        conn: asyncpg.Connection,
        workspace_id: str,
        invocation_id: str,
        owner_id: str,
        lease_seconds: float,
    ) -> ClaimOutcome:
        """Atomically take over an EXPIRED non-terminal row (CAS, one winner).

        Matches the OBSERVED generation (NULL-safe) and requires the lease
        to be expired in DATABASE time; only then is a NEW token minted and
        the attempt bumped. A rowcount of 0 means the row moved (another
        taker won or it went terminal) - re-read and re-classify.
        """
        for _ in range(_TAKEOVER_ATTEMPTS):
            observed = await conn.fetchrow(
                f"SELECT {_RECORD_COLUMNS}, "
                f"(lease_expires_at IS NULL OR lease_expires_at <= now()) "
                f"AS lease_expired FROM {self._table} "
                "WHERE workspace_id = $1 AND invocation_id = $2",
                workspace_id,
                invocation_id,
            )
            if observed is None:
                return ClaimOutcome("gone")
            record = self._record_from_row(observed)
            if record.terminal:
                return ClaimOutcome("replay", record)
            if not observed["lease_expired"]:
                # Database-authoritative: the owning generation still holds
                # a LIVE lease - never take over, never re-issue a mutation.
                return ClaimOutcome("in_progress", record)
            fence = new_fence(owner_id, attempt=record.attempt + 1)
            result = await conn.execute(
                f"UPDATE {self._table} "
                "SET owner_id = $3, fencing_token = $4, attempt = attempt + 1, "
                "    lease_expires_at = now() + make_interval(secs => $5), "
                "    updated_at = now() "
                "WHERE workspace_id = $1 AND invocation_id = $2 "
                "  AND status IN ('pending', 'created') "
                "  AND fencing_token IS NOT DISTINCT FROM $6 "
                "  AND (lease_expires_at IS NULL OR lease_expires_at <= now())",
                workspace_id,
                invocation_id,
                owner_id,
                fence.token,
                float(lease_seconds),
                record.fencing_token,
            )
            if result == "UPDATE 1":
                taken = await conn.fetchrow(
                    f"SELECT {_RECORD_COLUMNS} FROM {self._table} "
                    "WHERE workspace_id = $1 AND invocation_id = $2",
                    workspace_id,
                    invocation_id,
                )
                if taken is None:  # pragma: no cover
                    continue
                return ClaimOutcome("takeover", self._record_from_row(taken), fence)
            # Row moved under us; loop re-reads the facts.
        raise SandboxLedgerError(
            LEDGER_ERROR, "takeover: row kept moving under concurrent callers"
        )

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

    async def renew(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        fence: SandboxInvocationFence,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"UPDATE {self._table} "
                    "SET lease_expires_at = now() + make_interval(secs => $1), "
                    "    updated_at = now() "
                    "WHERE workspace_id = $2 AND invocation_id = $3 "
                    "  AND fencing_token = $4 AND owner_id = $5 AND attempt = $6 "
                    "  AND status IN ('pending', 'created')",
                    float(lease_seconds),
                    workspace_id,
                    invocation_id,
                    fence.token,
                    fence.owner_id,
                    fence.attempt,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(LEDGER_ERROR, f"renew failed: {exc}") from exc
        return result == "UPDATE 1"

    async def record_created(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        sandbox_id: str,
        fence: SandboxInvocationFence,
    ) -> None:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"UPDATE {self._table} "
                    "SET status = 'created', sandbox_id = $1, updated_at = now() "
                    "WHERE workspace_id = $2 AND invocation_id = $3 "
                    "  AND fencing_token = $4 AND owner_id = $5 AND attempt = $6 "
                    "  AND status = 'pending'",
                    sandbox_id,
                    workspace_id,
                    invocation_id,
                    fence.token,
                    fence.owner_id,
                    fence.attempt,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"record_created failed: {exc}"
            ) from exc
        if result == "UPDATE 0":
            raise SandboxOwnershipLost(
                "record_created: ownership lost or row not pending"
            )

    async def complete(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        status: str,
        fence: SandboxInvocationFence,
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
                    f"UPDATE {self._table} "
                    "SET status = $1, sandbox_id = COALESCE($2, sandbox_id), "
                    "    output = $3, error = $4, server_state = $5, "
                    "    completed_at = now(), lease_expires_at = NULL, "
                    "    updated_at = now() "
                    "WHERE workspace_id = $6 AND invocation_id = $7 "
                    "  AND fencing_token = $8 AND owner_id = $9 AND attempt = $10 "
                    "  AND status IN ('pending', 'created')",
                    status,
                    sandbox_id,
                    output,
                    error,
                    (
                        json.dumps(server_state, sort_keys=True)
                        if server_state is not None
                        else None
                    ),
                    workspace_id,
                    invocation_id,
                    fence.token,
                    fence.owner_id,
                    fence.attempt,
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"complete failed: {exc}"
            ) from exc
        if result == "UPDATE 0":
            raise SandboxOwnershipLost(
                "complete: ownership lost or row already terminal"
            )

    async def takeover(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> ClaimOutcome:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                return await self._takeover_loop(
                    conn, workspace_id, invocation_id, owner_id, lease_seconds
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(LEDGER_ERROR, f"takeover failed: {exc}") from exc

    async def list_expired(self, *, limit: int = 10) -> list[SandboxInvocationRecord]:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {_RECORD_COLUMNS} FROM {self._table} "
                    "WHERE status IN ('pending', 'created') "
                    "  AND (lease_expires_at IS NULL OR lease_expires_at <= now()) "
                    "ORDER BY updated_at ASC LIMIT $1",
                    int(limit),
                )
        except asyncpg.PostgresError as exc:
            raise SandboxLedgerError(
                LEDGER_ERROR, f"list_expired failed: {exc}"
            ) from exc
        return [self._record_from_row(row) for row in rows]
