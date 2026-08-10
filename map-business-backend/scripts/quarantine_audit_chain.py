"""Audit chain quarantine (R2-P1-03, repair direction 5).

When ``verify_audit_chain.py`` reports a broken link, this script moves the
broken suffix (first broken event and everything after it) into
``config_audit_events_quarantine`` and resets the chain head to the last
verified event, so the chain can continue growing from a verified prefix.

Guarantees:
- history is NEVER recomputed or rewritten — rows are moved byte-for-byte;
- the good prefix is untouched and stays verifiable;
- requires the migration DSN (the app role only has SELECT/INSERT on the
  audit tables);
- idempotent: if the chain verifies OK, nothing happens.

Usage (run from the project root; both forms work, R2-P2-04):
    MAP_CONTROL_MIGRATION_DSN=postgresql+asyncpg://map_migrator:...@host/db \
        uv run python -m scripts.quarantine_audit_chain
    MAP_CONTROL_MIGRATION_DSN=... uv run python scripts/quarantine_audit_chain.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# R2-P2-04: make `python scripts/quarantine_audit_chain.py` importable from
# a clean checkout (script execution puts only scripts/ on sys.path).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from app.api.audit_events import verify_chain_rows  # noqa: E402
from app.db.session import build_engine  # noqa: E402

_SELECT_ALL = (
    "SELECT id, workspace_id, resource_type, resource_id, action, "
    "actor_user_id, actor_subject, actor_roles, request_id, source_ip, "
    "user_agent, before_version, after_version, json_patch, before_hash, "
    "after_hash, status, failure_code, recovered, prev_entry_hash, "
    "entry_hash, ordinal, error_message, created_at "
    "FROM map_control.config_audit_events ORDER BY ordinal"
)

_MOVE_COLUMNS = (
    "workspace_id, resource_type, resource_id, action, actor_user_id, "
    "actor_subject, actor_roles, request_id, source_ip, user_agent, "
    "before_version, after_version, json_patch, before_hash, after_hash, "
    "status, failure_code, recovered, prev_entry_hash, entry_hash, "
    "ordinal, error_message, created_at"
)


async def _run() -> int:
    dsn = os.getenv("MAP_CONTROL_MIGRATION_DSN")
    if not dsn:
        print("ERROR: set MAP_CONTROL_MIGRATION_DSN (migration role DSN)")
        return 2
    engine = build_engine(dsn)
    async with engine.begin() as conn:
        rows = (await conn.execute(text(_SELECT_ALL))).all()
        count, broken_at = verify_chain_rows(rows)
        if broken_at is None:
            print(f"CHAIN_OK events={count}; nothing to quarantine")
            return 0
        broken_index = int(broken_at.split(":", 1)[0])
        broken_rows = rows[broken_index:]
        last_good = rows[broken_index - 1] if broken_index > 0 else None
        await conn.execute(
            text(
                "INSERT INTO map_control.config_audit_events_quarantine "
                f"(original_id, {_MOVE_COLUMNS}) "
                f"SELECT id, {_MOVE_COLUMNS} FROM map_control.config_audit_events "
                "WHERE ordinal >= :first_ordinal"
            ),
            {"first_ordinal": broken_rows[0].ordinal},
        )
        await conn.execute(
            text(
                "DELETE FROM map_control.config_audit_events "
                "WHERE ordinal >= :first_ordinal"
            ),
            {"first_ordinal": broken_rows[0].ordinal},
        )
        await conn.execute(
            text(
                "INSERT INTO map_control.config_audit_chain_head "
                "(chain_id, head_ordinal, head_entry_hash, updated_at) "
                "VALUES (1, :head_ordinal, :head_entry_hash, now()) "
                "ON CONFLICT (chain_id) DO UPDATE SET "
                "head_ordinal = EXCLUDED.head_ordinal, "
                "head_entry_hash = EXCLUDED.head_entry_hash, "
                "updated_at = now()"
            ),
            {
                "head_ordinal": broken_index,
                "head_entry_hash": last_good.entry_hash if last_good else "",
            },
        )
    await engine.dispose()
    print(
        f"QUARANTINED events={len(broken_rows)} from {broken_at}; "
        f"chain resumed at ordinal={broken_index} "
        f"(good prefix events={broken_index})"
    )
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
