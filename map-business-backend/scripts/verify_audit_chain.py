"""Audit chain verification (FIX-P1-AUDIT-01 / R2-P1-03).

Recomputes every config_audit_events entry_hash from the previous one —
walking the chain by ``ordinal`` and using the persisted ``error_message``
— and reports the first broken link (or OK). Read-only, re-runnable, and
never rewrites history.

If the chain is broken, quarantine the bad suffix (migrator DSN
required):
    MAP_CONTROL_MIGRATION_DSN=postgresql+asyncpg://map_migrator:...@... \
        uv run python -m scripts.quarantine_audit_chain

Usage (run from the project root; both forms work, R2-P2-04):
    MAP_CONTROL_DB_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map \
        uv run python -m scripts.verify_audit_chain
    MAP_CONTROL_DB_DSN=... uv run python scripts/verify_audit_chain.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# R2-P2-04: make `python scripts/verify_audit_chain.py` importable from a
# clean checkout — script execution puts only scripts/ on sys.path, so the
# project root must be added explicitly before importing `app`.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from app.api.audit_events import verify_chain_rows  # noqa: E402
from app.db.session import build_engine  # noqa: E402

_SQL = text(
    "SELECT id, workspace_id, resource_type, resource_id, action, "
    "actor_user_id, actor_subject, actor_roles, request_id, status, "
    "failure_code, before_hash, after_hash, json_patch, recovered, "
    "error_message, prev_entry_hash, entry_hash, ordinal "
    "FROM map_control.config_audit_events ORDER BY ordinal"
)


async def _run() -> int:
    engine = build_engine(
        os.getenv("MAP_CONTROL_DB_DSN", "postgresql+asyncpg://map:map@127.0.0.1:15432/map")
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(_SQL)).all()
    await engine.dispose()

    count, broken_at = verify_chain_rows(rows)
    if broken_at is not None:
        print(
            f"BROKEN_CHAIN at {broken_at} (events={count}); "
            "run scripts/quarantine_audit_chain.py with the migrator DSN "
            "to isolate the broken suffix — history is never rewritten"
        )
        return 1
    print(f"CHAIN_OK events={count}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
