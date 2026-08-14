#!/usr/bin/env python3
"""Acceptance-evidence validator (review R-08).

Stable entry point:

    python3 scripts/validate_acceptance_evidence.py \
        --profile TODO/acceptance-profile.yaml [--require-final]

Model. Git commits are immutable and evidence must describe a frozen code
commit, so the manifest directory (tmp/acceptance/<TASK>/<commit-sha>/...)
carries the FREEZE SHA the evidence is about:

- every non-superseded current manifest records the same implementation_sha
  (the single freeze sha) and lives under a directory named by that sha;
- coverage = every required AC has exactly one manifest at the freeze sha;
- manifests under any other sha must be marked superseded or failed
  (stale evidence never counts and never hides a missing current manifest);
- --require-final additionally proves the freeze sha is HEAD or an ancestor
  of HEAD and that every commit after it touched only tmp/acceptance/**
  (evidence-only tail) - i.e. the final HEAD carries the complete evidence
  set for its own frozen code.

Checks: AC uniqueness, dependency acyclicity, schema/structure, conditional
fields, real (non-placeholder) commands, status/exit-code consistency,
artifact sha256 re-hashing against the working tree, waiver expiry.

Exit codes: 0 = complete and consistent; 1 = validation failures;
2 = usage / parse errors. Called by scripts/release_gate.sh - it is the
single source of truth, no hand-counted totals allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DEFAULT = "TODO/acceptance-profile.yaml"

VALID_STATUSES = {
    "not-run",
    "running",
    "pass",
    "fail",
    "blocked",
    "superseded",
    "not-applicable-approved",
}

REQUIRED_FIELDS = (
    "schema_version",
    "task_id",
    "ac_id",
    "status",
    "baseline_sha",
    "implementation_sha",
    "environment_digest",
    "started_at",
    "finished_at",
    "command",
    "exit_code",
    "artifacts",
    "assertions",
    "finding_ids",
    "waiver_id",
    "waiver_owner",
    "waiver_expires_at",
    "not_applicable_reason",
    "blocked_reason",
    "blocker_owner",
    "superseded_by",
    "superseded_reason",
    "producer",
)

GIT_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER = re.compile(r"<[^>]+>")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def git_head() -> str:
    return git("rev-parse", "HEAD")


def git_is_ancestor(sha: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True,
    )
    return proc.returncode == 0


def git_changed_paths_since(sha: str) -> list[str]:
    if sha == git_head():
        return []
    return git("diff", "--name-only", sha, "HEAD").splitlines()


def load_profile(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"profile not found: {path}")
    try:
        import yaml  # type: ignore
    except ImportError:
        print("PyYAML is required (pip install pyyaml)", file=sys.stderr)
        raise SystemExit(2)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "task_registry" not in data:
        raise SystemExit(f"profile {path} lacks task_registry")
    return data


def expand_range(ids: list[str]) -> set[str]:
    """Expand acceptance_range entries: AC-X-01..AC-X-08 -> the full set."""
    result: set[str] = set()
    if len(ids) < 2:
        raise ValueError(f"acceptance_range must have two bounds: {ids}")
    start, end = ids[0], ids[-1]
    match_s = re.fullmatch(r"(AC-[A-Z0-9-]+-)(\d+)", start)
    match_e = re.fullmatch(r"(AC-[A-Z0-9-]+-)(\d+)", end)
    if not match_s or not match_e or match_s.group(1) != match_e.group(1):
        raise ValueError(f"cannot expand acceptance_range {ids}")
    prefix = match_s.group(1)
    lo, hi = int(match_s.group(2)), int(match_e.group(2))
    if lo > hi:
        raise ValueError(f"inverted acceptance_range {ids}")
    for number in range(lo, hi + 1):
        result.add(f"{prefix}{number:02d}")
    return result


def required_ac_by_task(profile: dict) -> dict[str, set[str]]:
    registry = profile["task_registry"]
    result: dict[str, set[str]] = {}
    for task, spec in registry.items():
        if "acceptance_ids" in spec:
            result[task] = {str(ac) for ac in spec["acceptance_ids"]}
        elif "acceptance_range" in spec:
            result[task] = expand_range([str(x) for x in spec["acceptance_range"]])
        else:
            raise ValueError(f"task {task} has neither acceptance_ids nor acceptance_range")
    return result


def check_dependency_cycles(profile: dict) -> list[str]:
    problems: list[str] = []
    registry = profile["task_registry"]
    dep_kinds = ("depends_on", "final_acceptance_depends_on",
                 "activation_depends_on", "soft_depends_on")
    graph: dict[str, list[str]] = {}
    for task, spec in registry.items():
        for kind in dep_kinds:
            for dep in spec.get(kind) or []:
                if dep not in registry:
                    problems.append(f"task {task}: unknown dependency {dep}")
                else:
                    graph.setdefault(task, []).append(dep)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, WHITE) == WHITE:
                visit(nxt)
            elif color.get(nxt) == GREY:
                cycle = stack[stack.index(nxt):] + [nxt]
                problems.append("dependency cycle: " + " -> ".join(cycle))
        stack.pop()
        color[node] = BLACK

    for node in graph:
        if color.get(node, WHITE) == WHITE:
            visit(node)
    return problems


def collect_manifests(tasks: set[str]) -> list[tuple[Path, str]]:
    """Return [(manifest_path, dir_sha)] under tmp/acceptance/<task>/<sha>/."""
    found: list[tuple[Path, str]] = []
    base = ROOT / "tmp" / "acceptance"
    if not base.is_dir():
        return found
    for task_dir in base.iterdir():
        if not task_dir.is_dir() or task_dir.name not in tasks:
            continue
        for sha_dir in task_dir.iterdir():
            if not sha_dir.is_dir():
                continue
            for ac_dir in sha_dir.iterdir():
                manifest = ac_dir / "evidence-manifest.json"
                if manifest.is_file():
                    found.append((manifest, sha_dir.name))
    return found


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def validate_manifest(
    manifest: Path, *, freeze_sha: str, verify_artifacts: bool
) -> list[str]:
    problems: list[str] = []
    rel = str(manifest.relative_to(ROOT))
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"{rel}: unreadable/invalid JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"{rel}: manifest must be a JSON object"]

    for field in REQUIRED_FIELDS:
        if field not in data:
            problems.append(f"{rel}: missing required field {field!r}")
            return problems

    if data["schema_version"] != "1.1.0":
        problems.append(f"{rel}: schema_version must be 1.1.0")
    if data["status"] not in VALID_STATUSES:
        problems.append(f"{rel}: unknown status {data['status']!r}")
    for oid_field in ("baseline_sha", "implementation_sha"):
        if not GIT_OID.fullmatch(str(data[oid_field])):
            problems.append(f"{rel}: {oid_field} is not a git OID")
    if not SHA256.fullmatch(str(data["environment_digest"])):
        problems.append(f"{rel}: environment_digest must be sha256 hex")
    for ts_field in ("started_at", "finished_at"):
        if parse_ts(str(data[ts_field])) is None:
            problems.append(f"{rel}: {ts_field} is not a valid timestamp")
    if not isinstance(data["command"], str) or not data["command"].strip():
        problems.append(f"{rel}: command is empty")
    elif PLACEHOLDER.search(data["command"]):
        problems.append(f"{rel}: command contains placeholder text")
    if not isinstance(data["exit_code"], int) or isinstance(data["exit_code"], bool):
        problems.append(f"{rel}: exit_code must be an integer")
    if not isinstance(data["artifacts"], list) or not data["artifacts"]:
        problems.append(f"{rel}: artifacts must be a non-empty list")
    if not isinstance(data["assertions"], list) or not data["assertions"]:
        problems.append(f"{rel}: assertions must be a non-empty list")
    if not isinstance(data["producer"], dict):
        problems.append(f"{rel}: producer must be an object")

    status = data["status"]
    if status == "blocked":
        if not (data["blocked_reason"] and data["blocker_owner"]):
            problems.append(f"{rel}: blocked requires blocked_reason and blocker_owner")
    elif data["blocked_reason"] is not None or data["blocker_owner"] is not None:
        problems.append(f"{rel}: blocked fields set for status {status}")
    if status == "superseded":
        if not (data["superseded_by"] and data["superseded_reason"]):
            problems.append(f"{rel}: superseded requires superseded_by and superseded_reason")
    elif data["superseded_by"] is not None or data["superseded_reason"] is not None:
        problems.append(f"{rel}: superseded fields set for status {status}")
    if status == "not-applicable-approved":
        if not (data["waiver_id"] and data["waiver_owner"] and data["not_applicable_reason"]):
            problems.append(f"{rel}: waiver requires waiver_id/waiver_owner/not_applicable_reason")
        expiry = parse_ts(str(data["waiver_expires_at"] or ""))
        if expiry is None:
            problems.append(f"{rel}: waiver_expires_at missing/invalid")
        elif expiry <= datetime.now(timezone.utc):
            problems.append(f"{rel}: waiver expired at {expiry.isoformat()}")
    elif data["waiver_id"] is not None or data["waiver_owner"] is not None \
            or data["waiver_expires_at"] is not None \
            or data["not_applicable_reason"] is not None:
        problems.append(f"{rel}: waiver fields set for status {status}")

    assertions = data["assertions"] if isinstance(data["assertions"], list) else []
    assertion_results = {
        str(item.get("result")) for item in assertions if isinstance(item, dict)
    }
    if status == "pass":
        if data["exit_code"] != 0:
            problems.append(f"{rel}: pass but exit_code != 0")
        if assertion_results - {"pass"}:
            problems.append(f"{rel}: pass but failing assertions present")
    if status == "fail":
        if data["exit_code"] == 0 and "fail" not in assertion_results:
            problems.append(f"{rel}: fail but exit_code==0 and no failing assertion")

    if data["implementation_sha"] == freeze_sha:
        # artifact hashes are re-verified only for CURRENT manifests
        # (historical manifests reference files that intentionally changed).
        for artifact in data["artifacts"]:
            if not isinstance(artifact, dict):
                problems.append(f"{rel}: artifact entry is not an object")
                continue
            path = ROOT / str(artifact.get("path") or "")
            if not path.is_file():
                problems.append(f"{rel}: artifact {artifact.get('path')} missing at HEAD")
                continue
            if not SHA256.fullmatch(str(artifact.get("sha256") or "")):
                problems.append(f"{rel}: artifact {artifact.get('path')} sha256 malformed")
                continue
            actual = sha256_file(path)
            if actual != artifact["sha256"]:
                problems.append(
                    f"{rel}: artifact {artifact.get('path')} hash mismatch "
                    f"(recorded {str(artifact['sha256'])[:12]}..., actual {actual[:12]}...)"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=PROFILE_DEFAULT)
    parser.add_argument("--require-final", action="store_true")
    parser.add_argument("--task", default=None, help="validate a single task")
    parser.add_argument("--ac", default=None, help="validate a single AC (needs --task)")
    args = parser.parse_args(argv)

    try:
        head = git_head()
        profile = load_profile(ROOT / args.profile)
        ac_by_task = required_ac_by_task(profile)
    except (SystemExit, ValueError, subprocess.CalledProcessError) as exc:
        print(f"evidence validator error: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    problems.extend(check_dependency_cycles(profile))

    seen: dict[str, str] = {}
    for task, acs in ac_by_task.items():
        for ac in acs:
            if ac in seen:
                problems.append(f"AC {ac} declared by both {seen[ac]} and {task}")
            seen[ac] = task

    manifests = collect_manifests(set(ac_by_task))
    by_ac: dict[str, list[tuple[Path, str]]] = {}
    for manifest, dir_sha in manifests:
        by_ac.setdefault(manifest.parent.name, []).append((manifest, dir_sha))

    # Determine the freeze sha: the implementation_sha of every
    # non-superseded manifest must agree on exactly one commit.
    freeze_candidates: set[str] = set()
    for manifest, dir_sha in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("status") != "superseded":
            if isinstance(data.get("implementation_sha"), str):
                freeze_candidates.add(data["implementation_sha"])
    freeze_sha = None
    if len(freeze_candidates) == 1:
        freeze_sha = freeze_candidates.pop()
    elif len(freeze_candidates) > 1:
        problems.append(
            f"current manifests disagree on the implementation sha: {sorted(freeze_candidates)}"
        )

    # Honesty rule: manifests under a non-freeze sha must be superseded/fail.
    if freeze_sha:
        for manifest, dir_sha in manifests:
            if dir_sha != freeze_sha:
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(data, dict) and data.get("status") not in {"superseded", "fail"}:
                    problems.append(
                        f"{manifest.relative_to(ROOT)}: stale manifest (dir sha "
                        f"{dir_sha[:12]}) not marked superseded/failed"
                    )

    if args.require_final:
        if freeze_sha is None:
            problems.append("--require-final: no current (non-superseded) evidence exists")
        else:
            if freeze_sha != head and not git_is_ancestor(freeze_sha):
                problems.append(
                    f"--require-final: freeze sha {freeze_sha[:12]} is not HEAD "
                    "or an ancestor of HEAD"
                )
            changed = git_changed_paths_since(freeze_sha)
            non_evidence = [
                p for p in changed
                if not p.startswith("tmp/acceptance/")
            ]
            if non_evidence:
                problems.append(
                    "--require-final: commits after the freeze sha touch "
                    f"non-evidence paths: {non_evidence[:5]}"
                )

    expected: dict[str, str] = {}
    for task, acs in ac_by_task.items():
        for ac in acs:
            expected[ac] = task
    if args.task:
        if args.task not in ac_by_task:
            print(f"unknown task {args.task}", file=sys.stderr)
            return 2
        expected = {ac: args.task for ac in ac_by_task[args.task]}
    if args.ac:
        if args.ac not in expected:
            print(f"unknown AC {args.ac}", file=sys.stderr)
            return 2
        expected = {args.ac: expected[args.ac]}

    for ac, task in sorted(expected.items()):
        if freeze_sha is None:
            problems.append(
                f"AC {ac} (task {task}): no current evidence (no freeze sha)"
            )
            continue
        current = [m for m, sha in by_ac.get(ac, []) if sha == freeze_sha]
        if not current:
            problems.append(
                f"AC {ac} (task {task}): no current evidence at "
                f"tmp/acceptance/{task}/{freeze_sha[:12]}.../"
            )
            continue
        if len(current) > 1:
            problems.append(f"AC {ac}: duplicate current manifests: {current}")
        for manifest in current:
            problems.extend(
                validate_manifest(manifest, freeze_sha=freeze_sha, verify_artifacts=True)
            )
        # every other manifest for this AC is historical: structure only
        for manifest, sha in by_ac.get(ac, []):
            if sha == freeze_sha:
                continue
            problems.extend(
                validate_manifest(manifest, freeze_sha=freeze_sha, verify_artifacts=False)
            )

    if args.ac:
        if args.ac not in expected or problems:
            return 1
        current = [m for m, sha in by_ac.get(args.ac, []) if sha == freeze_sha]
        if not current:
            return 1
        data = json.loads(current[0].read_text(encoding="utf-8"))
        return 0 if data["status"] == "pass" else 1

    total = len(expected)
    if problems:
        print(
            f"evidence validation FAILED ({len(problems)} problem(s), "
            f"{total} required AC(s) checked, freeze sha "
            f"{freeze_sha[:12] if freeze_sha else 'NONE'}):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"evidence validation OK: {total} required AC(s) covered by unique "
        f"current manifests at freeze sha {freeze_sha[:12]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
