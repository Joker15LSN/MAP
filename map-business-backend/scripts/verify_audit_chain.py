"""Audit chain verification (FIX-P1-AUDIT-01).

Recomputes every config_audit_events entry_hash from the previous one and
reports the first broken link (or OK). Read-only, re-runnable.

Usage:
    MAP_CONTROL_DB_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map \
        uv run python scripts/verify_audit_chain.py
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from app.db.session import build_engine
from app.services.config_mutation import audit_record_payload, compute_entry_hash

_SQL = text(
    "SELECT id, workspace_id, resource_type, resource_id, action, "
    "actor_user_id, actor_subject, actor_roles, request_id, status, "
    "failure_code, before_hash, after_hash, json_patch, recovered, "
    "prev_entry_hash, entry_hash "
    "FROM map_control.config_audit_events ORDER BY created_at, id"
)


async def _run() -> int:
    engine = build_engine(
        os.getenv("MAP_CONTROL_DB_DSN", "postgresql+asyncpg://map:map@127.0.0.1:15432/map")
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(_SQL)).all()
    await engine.dispose()

    prev = ""
    for index, row in enumerate(rows):
        record = audit_record_payload(
            workspace_id=row.workspace_id,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            action=row.action,
            actor_user_id=row.actor_user_id,
            actor_subject=row.actor_subject,
            actor_roles=list(row.actor_roles or []),
            request_id=row.request_id,
            status=row.status,
            failure_code=row.failure_code,
            before_hash=row.before_hash,
            after_hash=row.after_hash,
            json_patch=row.json_patch,
            recovered=row.recovered,
            error_message=None,
        )
        expected = compute_entry_hash(prev, record)
        if row.entry_hash != expected or row.prev_entry_hash != (prev or None):
            print(
                f"BROKEN_CHAIN at index={index} event_id={row.id} "
                f"expected={expected} stored={row.entry_hash}"
            )
            return 1
        prev = row.entry_hash
    print(f"CHAIN_OK events={len(rows)}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
