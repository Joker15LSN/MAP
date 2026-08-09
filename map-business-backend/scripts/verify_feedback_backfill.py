"""Feedback legacy backfill verification (FIX-P1-FEEDBACK-01).

Run after the ``6f8a0c2e4d5b`` migration (which already backfills rating/
reason_other from legacy kind/reason rows). This script reports:

- legacy row count (kind IN thumbs_up/thumbs_down),
- new-format row count (rating IS NOT NULL),
- deterministic conflicts: legacy rows that carry BOTH kinds for one
  message (they must be reviewed by a human; nothing is silently chosen),
- a stable hash of legacy rows so reruns can prove no drift.

Re-runnable: read-only.

Usage:
    MAP_CONTROL_DB_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map \
        uv run python scripts/verify_feedback_backfill.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from sqlalchemy import text

from app.db.session import build_engine

DSN = os.getenv(
    "MAP_CONTROL_DB_DSN", "postgresql+asyncpg://map:map@127.0.0.1:15432/map"
)


async def _run() -> int:
    engine = build_engine(DSN)
    async with engine.connect() as conn:
        legacy = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM map_control.message_feedback "
                    "WHERE kind IN ('thumbs_up', 'thumbs_down')"
                )
            )
        ).scalar_one()
        new_format = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM map_control.message_feedback "
                    "WHERE rating IS NOT NULL"
                )
            )
        ).scalar_one()
        conflict_rows = (
            await conn.execute(
                text(
                    "SELECT message_id, count(DISTINCT kind) AS kinds "
                    "FROM map_control.message_feedback "
                    "WHERE kind IN ('thumbs_up', 'thumbs_down') "
                    "GROUP BY message_id HAVING count(DISTINCT kind) > 1 "
                    "ORDER BY message_id"
                )
            )
        ).all()
        digest_rows = (
            await conn.execute(
                text(
                    "SELECT id, message_id, kind, reason, rating, reason_other "
                    "FROM map_control.message_feedback "
                    "WHERE kind IS NOT NULL OR rating IS NOT NULL "
                    "ORDER BY id"
                )
            )
        ).all()
    await engine.dispose()

    digest = hashlib.sha256()
    for row in digest_rows:
        digest.update(
            json.dumps(list(row), ensure_ascii=False, sort_keys=True).encode("utf-8")
        )

    print(f"legacy_rows={legacy}")
    print(f"new_format_rows={new_format}")
    print(f"conflicts(messages with both kinds)={len(conflict_rows)}")
    for row in conflict_rows:
        print(f"  conflict message_id={row.message_id} kinds={row.kinds}")
    print(f"legacy_hash_sha256={digest.hexdigest()}")

    if len(conflict_rows) > 0:
        print(
            "WARNING: conflicting legacy rows require a deterministic human "
            "decision (FIX-P1-FEEDBACK-01 9.5); nothing was auto-chosen."
        )
        return 1
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
