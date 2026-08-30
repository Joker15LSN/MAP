"""Regenerate the OpenAPI contract snapshot (R2-P2-04).

Regeneration is an INTENTIONAL, reviewable change: the diff of
``tests/contracts/openapi_snapshot.json`` in the same commit is what a
reviewer approves. Any schema change that lands without a snapshot
regeneration fails ``test_openapi_full_schema_matches_snapshot``.

Usage (from map-business-backend/):
    uv run python tests/contracts/gen_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Explicit test settings: never inherit a developer shell's MAP_* vars.

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402

SNAPSHOT_PATH = Path(__file__).parent / "openapi_snapshot.json"


def main() -> None:
    app = create_app(
        settings=Settings(auth_mode="dev")
    )
    schema = app.openapi()
    SNAPSHOT_PATH.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"snapshot written: {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
