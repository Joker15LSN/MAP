#!/usr/bin/env python3
"""R7-P2-03: resolve the implementation (code-freeze) commit for evidence.

The release validator's freeze model says: every commit AFTER the freeze
sha may touch ONLY ``tmp/acceptance/**`` (evidence-only tail). The
protected-CI ``gate-final`` job must therefore attest evidence against the
FIRST commit walking back from HEAD whose change set is not evidence-only
- never against HEAD itself, which on a protected-branch push is normally
the evidence re-freeze commit.

Resolution (fail-closed, no commit-message convention required):

    walk HEAD, HEAD^, HEAD^^, ... in git order;
    for each commit compute its changed paths with ``git diff-tree``;
    the first commit that changes ANY path outside ``tmp/acceptance/**``
    is the implementation commit;
    if every commit (including the root) is evidence-only, HEAD is
    returned.

Usage:
    python3 scripts/resolve_evidence_implementation_sha.py [--repo DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

EVIDENCE_PREFIX = "tmp/acceptance/"


def git_text(repo_root: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def changed_paths_for_commit(repo_root: Path, sha: str) -> list[str]:
    """Changed paths of ONE commit (file additions/modifications only)."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            sha,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def resolve_implementation_sha(repo_root: Path) -> str:
    head = git_text(repo_root, "rev-parse", "HEAD")
    if not head:
        raise RuntimeError("repository has no HEAD")
    commits = git_text(repo_root, "rev-list", "HEAD")
    if commits is None:
        raise RuntimeError("could not walk commit history")
    for sha in commits.splitlines():
        if not sha.strip():
            continue
        paths = changed_paths_for_commit(repo_root, sha.strip())
        if not paths:
            # Root commit whose diff-tree emits nothing unless --root is
            # used; an empty change set cannot describe product code, so
            # treat it as the implementation commit.
            return sha.strip()
        if any(not path.startswith(EVIDENCE_PREFIX) for path in paths):
            return sha.strip()
    return head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    args = parser.parse_args(argv)
    try:
        sha = resolve_implementation_sha(Path(args.repo).resolve())
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"[evidence-impl-sha] FAILED: {exc!r}", file=sys.stderr)
        return 1
    print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
