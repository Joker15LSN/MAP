#!/usr/bin/env python3
"""R2-P2-02 bundle size gate (release gate, CI-enforced).

Verifies every emitted ``.js`` asset of each frontend build against the
budgets in ``scripts/bundle-budget.json``:

- the ENTRY chunk (the module referenced by ``dist/index.html``) and every
  other lazy/shared CHUNK are checked separately, each against both a RAW
  and a GZIP ceiling;
- any breach fails the gate (exit 1) with the offending asset and the exact
  ceiling. Raising a budget requires a numbered approval recorded in
  ``map-business-backend/QUALITY_FIX_RECORD.md`` — budgets are measured
  baselines that prevent regression, not aspirational targets.

Usage (from the repository root)::

    python3 scripts/check_bundle_size.py            # all configured projects
    python3 scripts/check_bundle_size.py map-business-frontend

Exit codes: 0 = within budget, 1 = budget breach or missing dist.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILE = REPO_ROOT / "scripts" / "bundle-budget.json"


def gzip_size(path: Path) -> int:
    # level 6 ≈ the nginx/CDN default used when serving pre-compressed assets
    return len(gzip.compress(path.read_bytes(), compresslevel=6))


def entry_assets(index_html: Path) -> set[str]:
    """Assets referenced by the built index.html (script/modulepreload)."""
    text = index_html.read_text(encoding="utf-8")
    return {name for name in re.findall(r'assets/([^"\']+\.js)', text)}


def check_project(name: str, config: dict) -> list[str]:
    dist = REPO_ROOT / config["dist"]
    assets_dir = dist / "assets"
    index_html = dist / "index.html"
    if not assets_dir.is_dir() or not index_html.is_file():
        return [f"{name}: dist not built at {dist} (run `npm run build` first)"]

    entries = entry_assets(index_html)
    failures: list[str] = []
    print(f"[bundle] {name}")
    for path in sorted(assets_dir.glob("*.js")):
        kind = "entry" if path.name in entries else "chunk"
        budget = config[kind]
        raw = path.stat().st_size
        gz = gzip_size(path)
        status = "ok"
        if raw > budget["raw_max"]:
            status = f"RAW OVER BUDGET ({raw} > {budget['raw_max']})"
            failures.append(f"{name}: {path.name} [{kind}] {status}")
        if gz > budget["gzip_max"]:
            status = f"GZIP OVER BUDGET ({gz} > {budget['gzip_max']})"
            failures.append(f"{name}: {path.name} [{kind}] {status}")
        print(
            f"  {kind:<5} {path.name:<28} raw={raw:>9} gzip={gz:>8} "
            f"limits(raw={budget['raw_max']}, gzip={budget['gzip_max']}) {status}"
        )
    if not entries:
        failures.append(f"{name}: no entry script found in {index_html}")
    return failures


def main() -> int:
    budgets = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    projects = budgets["projects"]
    selected = sys.argv[1:] or list(projects)
    unknown = [name for name in selected if name not in projects]
    if unknown:
        print(f"unknown project(s): {unknown}; configured: {sorted(projects)}", file=sys.stderr)
        return 1

    failures: list[str] = []
    for name in selected:
        failures.extend(check_project(name, projects[name]))

    if failures:
        print("\n[bundle] GATE FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print(
            "  Raising a budget requires a numbered approval recorded in "
            "map-business-backend/QUALITY_FIX_RECORD.md.",
            file=sys.stderr,
        )
        return 1
    print("\n[bundle] all assets within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
