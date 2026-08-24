#!/usr/bin/env python3
"""R5-P2-02: the ONE tested source-control snapshot used by both the
release gate and the E2E runner.

The fifth-round review proved that two hand-rolled porcelain parsers had
drifted into different bugs: the E2E runner called ``.strip()`` on the
whole porcelain output and then sliced a fixed ``[3:]`` — truncating a
real path character (``e2e/browser_e2e.py`` became ``2e/browser_e2e.py``);
the gate parsed default porcelain line-by-line without disabling
``core.quotePath``, so Chinese documentation paths arrived as quoted
octal escapes and the docs-only rule never matched. This module replaces
both with a single NUL-based parser whose output is byte-identical to
the real repository-relative paths, covered by
``map-business-backend/tests/test_source_control_snapshot.py``.

Rules baked in:

- ``git -c core.quotepath=false status --porcelain=v1 -z`` parsed as
  BYTES split on NUL. No whole-output ``.strip()`` and no fixed-column
  slicing — the first line, leading spaces, Chinese paths, rename double
  paths and untracked files are all handled correctly.
- docs/product classification runs ONLY on parsed repo-relative paths;
  the rule lives in ONE place (``is_docs_path``). R6-P2-01: a RENAME is
  "old path deleted + new path added", so BOTH paths of a rename drive
  classification (``affected_paths_for``) — renaming a product file into
  a docs directory can no longer bypass the clean-product gate; a COPY
  leaves its origin in place, so only the destination is affected while
  the origin stays in the artifact for audit. R7-P2-01: porcelain rename
  can sit in EITHER XY column — ``git mv`` stages it (``XY="R "``) while
  ``mv`` + ``git add -N`` leaves it in the worktree column (``XY=" R"``);
  classification checks ``"R" in xy`` so both forms behave identically.
- a dirty working tree additionally records working-tree content
  evidence (``diff_head_sha256`` over ``git diff HEAD`` plus a per-file
  sha256 manifest of every untracked file), so a non-final artifact can
  still prove WHAT was tested; a FINAL run must happen on a clean
  product tree where commit SHA/tree alone describe the product code.
- R7-P2-03 / S8-02: acceptance evidence is NOT product code, but the
  exemption is deliberately narrow. A path must already be tracked at
  ``HEAD`` and have a known evidence shape (manifest, log, or an artifact
  referenced by its committed manifest). Merely placing an untracked or
  unexpected file below ``tmp/acceptance/`` never bypasses the clean-product
  gate. The protected-CI attest step can therefore re-attest pass manifests
  IN PLACE without opening a prefix-wide exemption.

CLI:

    python3 scripts/source_control.py --json [--repo DIR] [--require-clean-product]

Exits 0 on success, 2 when ``--require-clean-product`` is set and any
PRODUCT file is dirty (docs-only dirtiness never triggers it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# R4-P2-03 / R5-P2-02: paths whose working-tree changes are documentation,
# not product code. CENTRAL classification rule — the gate and the E2E
# runner must never re-implement it.
DOCS_PREFIXES = ("TODO/", "SPEC/", ".qoder/", ".reasonix/", ".understand-anything/")

# R7-P2-03 / S8-02: the protected-CI attest step re-signs tracked pass
# manifests in place. Do not make the whole prefix evidence: an untracked
# ``tmp/acceptance/evil.py`` or tracked-but-unknown file must remain product
# dirt. Shape validation is combined with HEAD tracking in ``snapshot``.
EVIDENCE_PREFIX = "tmp/acceptance/"


def is_docs_path(path: str) -> bool:
    """True when a repo-relative path is documentation, not product code."""
    return path.startswith(DOCS_PREFIXES) or path.endswith(".md")


def _evidence_dir(path: str) -> str | None:
    """Return ``tmp/acceptance/<task>/<sha>/<ac>`` for a shaped path."""
    if not path.startswith(EVIDENCE_PREFIX):
        return None
    parts = path.split("/")
    if len(parts) < 6:
        return None
    freeze_sha = parts[3]
    if len(freeze_sha) != 40 or any(
        ch not in "0123456789abcdef" for ch in freeze_sha
    ):
        return None
    if not parts[2] or not parts[4]:
        return None
    return "/".join(parts[:5])


def is_evidence_path(
    path: str,
    *,
    head_tracked_paths: set[str],
    manifest_artifact_paths: set[str],
) -> bool:
    """True only for tracked, structurally known acceptance evidence.

    The committed ``HEAD`` is the trust boundary. Index-only additions and
    untracked files are intentionally excluded, even when their names mimic
    evidence. Besides the manifest and ``logs/**`` shapes, a committed
    manifest may explicitly name another artifact inside its own AC
    directory; those exact tracked paths are accepted too.
    """
    if path not in head_tracked_paths:
        return False
    evidence_dir = _evidence_dir(path)
    if evidence_dir is None:
        return False
    relative = path.removeprefix(evidence_dir + "/")
    return (
        relative == "evidence-manifest.json"
        or relative.startswith("logs/")
        or path in manifest_artifact_paths
    )


def parse_porcelain_z(raw: bytes) -> list[dict]:
    """Parse ``git status --porcelain=v1 -z`` output BYTES.

    Each record is ``XY + " " + path`` terminated by NUL; rename/copy
    records are followed by the ORIGINAL path as an extra NUL record.
    Paths are decoded byte-exact (``core.quotepath=false`` keeps UTF-8
    literal). Never ``.strip()`` the whole buffer and never slice fixed
    columns — both were proven fifth-round defects.
    """
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()  # trailing terminator
    entries: list[dict] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 3:
            raise ValueError(f"malformed porcelain record: {record!r}")
        xy = record[:2].decode("ascii")
        path = record[3:].decode("utf-8")
        orig_path = None
        if "R" in xy or "C" in xy:
            index += 1
            if index >= len(records):
                raise ValueError(f"rename/copy entry missing origin path: {path!r}")
            orig_path = records[index].decode("utf-8")
        entries.append({"xy": xy, "path": path, "orig_path": orig_path})
        index += 1
    return entries


def affected_paths_for(entry: dict) -> list[str]:
    """Repo-relative paths whose state changed for ONE porcelain entry.

    R6-P2-01: a RENAME is "old path deleted + new path added" — BOTH the
    destination (``path``) and the origin (``orig_path``) must drive
    docs/product classification, otherwise ``git mv app.py
    TODO/app.py.md`` would masquerade a product deletion as a docs-only
    change and bypass the clean-product final gate. A COPY leaves its
    origin in place, so only the destination is affected; the origin
    still stays in the entry (and thus the artifact) for audit.

    R7-P2-01: porcelain ``XY`` is the index column ``X`` plus the
    worktree column ``Y``, and a rename legally appears in EITHER one —
    ``git mv`` produces ``XY="R "`` while ``mv app.py TODO/app.py.md &&
    git add -N TODO/app.py.md`` stably produces ``XY=" R"`` in a real
    repository. The check is therefore ``"R" in xy``; inspecting only
    ``xy[0]`` would let the worktree-side form bypass the gate while
    ``orig_path`` is silently dropped.
    """
    paths = [entry["path"]]
    if "R" in entry["xy"] and entry.get("orig_path"):
        paths.append(entry["orig_path"])
    return paths


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, check=True
    )
    return result.stdout


def _git_text(repo_root: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def _head_tracked_paths(repo_root: Path) -> set[str]:
    """Repo-relative files committed at HEAD (index-only files excluded)."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            "HEAD",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return set()
    return {
        path for path in result.stdout.decode("utf-8").split("\0") if path
    }


def _head_manifest_artifact_paths(
    repo_root: Path, affected_paths: list[str], head_tracked_paths: set[str]
) -> set[str]:
    """Load exact in-directory artifact paths from relevant HEAD manifests.

    Only manifests adjacent to affected acceptance paths are read. Artifact
    references are accepted only inside that manifest's own AC directory;
    a manifest can never exempt product code elsewhere in the repository.
    """
    artifacts: set[str] = set()
    manifest_paths = set()
    for path in affected_paths:
        evidence_dir = _evidence_dir(path)
        if evidence_dir is None:
            continue
        manifest_path = evidence_dir + "/evidence-manifest.json"
        if manifest_path in head_tracked_paths:
            manifest_paths.add(manifest_path)

    for manifest_path in manifest_paths:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"HEAD:{manifest_path}"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            manifest = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("artifacts"), list
        ):
            continue
        evidence_dir = manifest_path.rsplit("/", 1)[0]
        for artifact in manifest["artifacts"]:
            if not isinstance(artifact, dict) or not isinstance(
                artifact.get("path"), str
            ):
                continue
            raw_path = artifact["path"]
            parsed = PurePosixPath(raw_path)
            if parsed.is_absolute() or ".." in parsed.parts:
                continue
            normalized = parsed.as_posix()
            if normalized.startswith(evidence_dir + "/"):
                artifacts.add(normalized)
    return artifacts


def _untracked_manifest(repo_root: Path) -> tuple[list[dict], str]:
    """sha256 manifest of every untracked (non-ignored) file + combined
    hash — the working-tree content evidence for non-final runs."""
    raw = _git_bytes(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = [p for p in raw.decode("utf-8").split("\0") if p]
    manifest: list[dict] = []
    for rel in sorted(paths):
        file_path = repo_root / rel
        if not file_path.is_file():
            continue  # directory entry or raced deletion — files only
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        manifest.append({"path": rel, "sha256": digest})
    combined = hashlib.sha256(
        "".join(f"{m['path']}:{m['sha256']}\n" for m in manifest).encode("utf-8")
    ).hexdigest()
    return manifest, combined


def snapshot(repo_root: Path) -> dict:
    """Self-describing source-control evidence for gate/E2E artifacts.

    ``affected_paths`` is the DEDUPLICATED set of every path whose state
    changed — rename entries contribute BOTH paths (R6-P2-01); the gate
    and the E2E runner consume this one set, so they can never classify
    differently. ``dirty_files`` mirrors it for compatibility,
    ``dirty_product`` filters out docs AND narrowly classified tracked
    acceptance evidence (R7-P2-03/S8-02), ``docs_only_dirty`` is true only
    when the tree is dirty AND every affected path is non-product, and
    ``evidence_only_dirty`` is true when at least one affected path is
    tracked, structurally known evidence. All
    paths are PARSED repo-relative paths, byte-identical to what git
    reports. A dirty tree additionally carries ``diff_head_sha256`` and
    the untracked-file content manifest (R5-P2-02 content evidence).
    """
    porcelain = _git_bytes(
        repo_root,
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
    )
    entries = parse_porcelain_z(porcelain)
    affected_paths = sorted(
        {path for entry in entries for path in affected_paths_for(entry)}
    )
    head_tracked_paths = _head_tracked_paths(repo_root)
    manifest_artifact_paths = _head_manifest_artifact_paths(
        repo_root, affected_paths, head_tracked_paths
    )
    dirty_evidence = [
        path
        for path in affected_paths
        if is_evidence_path(
            path,
            head_tracked_paths=head_tracked_paths,
            manifest_artifact_paths=manifest_artifact_paths,
        )
    ]
    dirty_evidence_set = set(dirty_evidence)
    dirty_product = [
        path
        for path in affected_paths
        if not is_docs_path(path) and path not in dirty_evidence_set
    ]
    dirty_docs = [path for path in affected_paths if is_docs_path(path)]
    docs_only_dirty = bool(affected_paths) and not dirty_product
    evidence_only_dirty = (
        bool(affected_paths) and not dirty_product and bool(dirty_evidence)
    )
    result = {
        "git_sha": _git_text(repo_root, "rev-parse", "HEAD"),
        "git_tree": _git_text(repo_root, "rev-parse", "HEAD^{tree}"),
        "branch": _git_text(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(affected_paths),
        "affected_paths": affected_paths,
        "dirty_files": affected_paths,
        "dirty_docs": dirty_docs,
        "dirty_evidence": dirty_evidence,
        "dirty_product": dirty_product,
        "docs_only_dirty": docs_only_dirty,
        "evidence_only_dirty": evidence_only_dirty,
        "entries": entries,
        # release_gate.sh may invoke the macOS system Python 3.9, where
        # datetime.UTC does not exist yet.
        "captured_utc": datetime.now(timezone.utc).isoformat(  # noqa: UP017
            timespec="seconds"
        ),
    }
    if affected_paths:
        result["diff_head_sha256"] = hashlib.sha256(
            _git_bytes(repo_root, "diff", "HEAD")
        ).hexdigest()
        manifest, combined = _untracked_manifest(repo_root)
        result["untracked_manifest"] = manifest
        result["untracked_sha256"] = combined
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="R5-P2-02 shared NUL-safe git source-control snapshot"
    )
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="print the full snapshot")
    parser.add_argument(
        "--require-clean-product",
        action="store_true",
        help="exit 2 when any PRODUCT file is dirty (docs/evidence-only tolerated)",
    )
    args = parser.parse_args(argv)

    try:
        result = snapshot(Path(args.repo).resolve())
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"[source-control] FAILED: {exc!r}", file=sys.stderr)
        return 1

    if args.require_clean_product and result["dirty_product"]:
        print(
            "[source-control] refusing: dirty PRODUCT working tree "
            "(commit the product changes first):",
            file=sys.stderr,
        )
        for path in result["dirty_product"]:
            print(f"  - {path}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"sha={result['git_sha']} tree={result['git_tree']} "
            f"branch={result['branch']} dirty={len(result['dirty_files'])} "
            f"product_dirty={len(result['dirty_product'])} "
            f"evidence_dirty={len(result['dirty_evidence'])} "
            f"docs_only_dirty={result['docs_only_dirty']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
