"""Dev helper (not collected as a test): recompute runtime_config_hash for all fixtures.

Usage (from map_core):
    python -m tests.golden.fill_hashes
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAP_CORE = Path(__file__).resolve().parents[2]
TESTS = MAP_CORE / "tests"
sys.path.insert(0, str(MAP_CORE))
sys.path.insert(0, str(TESTS))

from golden import harness  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def main() -> None:
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        digest = harness.compute_runtime_hash(data)
        data["runtime_config_hash"] = digest
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{path.name}: {digest}")


if __name__ == "__main__":
    main()
