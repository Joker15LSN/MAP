#!/usr/bin/env python3
"""Acceptance-evidence retention index (Step 10).

Read-only by design: this script NEVER deletes evidence files. It records
which freeze is current, which historical freezes are superseded and the
exact archive command an owner must run once immutable storage is ready.

Usage:

    python3 scripts/evidence_retention.py --repo . --out tmp/acceptance/index.json
    python3 scripts/evidence_retention.py --repo . --archive-candidates --archive-uri s3://map-evidence/acceptance

Policy: TODO/retention/acceptance-evidence-retention.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE_ROOT = "tmp/acceptance"
SCHEMA_VERSION = "1.0.0"


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _manifests_by_freeze(repo: Path) -> dict[str, dict[str, int]]:
    base = repo / EVIDENCE_ROOT
    freezes: dict[str, dict[str, int]] = {}
    for manifest in base.glob("*/*/*/evidence-manifest.json"):
        freeze_sha = manifest.parent.parent.name
        task = manifest.parent.parent.parent.name
        ac = manifest.parent.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entry = freezes.setdefault(freeze_sha, {"manifest_count": 0, "superseded": 0})
        entry["manifest_count"] += 1
        if data.get("status") == "superseded":
            entry["superseded"] += 1
        entry.setdefault("example", f"{task}/{ac}")
    return freezes


def build_index(repo: Path, freeze_sha: str | None) -> dict:
    freeze_sha = freeze_sha or _git_head(repo)
    freezes = _manifests_by_freeze(repo)
    if freeze_sha not in freezes:
        resolved = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{freeze_sha}^{{commit}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if resolved in freezes:
            freeze_sha = resolved
    current = freezes.get(freeze_sha)
    historical = sorted(
        (
            sha
            for sha, counts in freezes.items()
            if sha != freeze_sha
        ),
    )
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
            EVIDENCE_ROOT,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "freeze_sha": freeze_sha,
        "current": current,
        "historical_freeze_shas": historical,
        "tracked_file_count": len(tracked),
        "archive_command": (
            "tar -C %s -czf acceptance-history-<date>.tar.gz "
            "--exclude='%s/%s' %s"
            % (
                repo,
                EVIDENCE_ROOT,
                freeze_sha,
                " ".join(f"{EVIDENCE_ROOT}/{sha}" for sha in historical),
            )
            if historical
            else None
        ),
        "policy": "TODO/retention/acceptance-evidence-retention.md",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out", default="tmp/acceptance/index.json")
    parser.add_argument("--freeze-sha", default=None)
    parser.add_argument("--archive-candidates", action="store_true")
    parser.add_argument("--archive-uri", default=None)
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    index = build_index(repo, args.freeze_sha)
    out = repo / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"evidence retention index: freeze={index['freeze_sha'][:12]} "
        f"current_manifests={index['current']['manifest_count'] if index['current'] else 0} "
        f"historical_freezes={len(index['historical_freeze_shas'])}"
    )
    if args.archive_candidates:
        uri = args.archive_uri or "<ARCHIVE_URI>"
        print(f"archive command for owner (no local deletion): {index['archive_command']}")
        print(f"upload target: {uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
