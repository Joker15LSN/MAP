#!/usr/bin/env python3
"""Acceptance-evidence validator (review R-08, second-round review S2-01).

Two EXPLICIT modes - structural integrity and release eligibility:

- structure (default): every required AC has exactly one current manifest at
  the freeze sha; manifests are schema-valid (including the JSON Schema's
  ``additionalProperties`` constraints), carry consistent
  task_id/ac_id/implementation_sha vs. their directory path, pass waiver
  expiry, artifact sha256 re-hash, status/exit-code consistency and
  producer/time-order checks. Exit 0 means the evidence set is structurally
  complete and consistent - it does NOT say anything about releasability.
- eligibility (--require-final): everything structure mode checks, PLUS
  every required AC's current manifest must be ``pass`` or a policy-approved
  AND unexpired ``not-applicable-approved`` waiver. blocked/fail/running/
  not-run/superseded make the release FAIL, with a per-status count and the
  explicit list of non-releasable ACs printed. The FINAL release gate MUST
  use this mode; anything else is not release evidence.

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

Exit codes: 0 = complete and consistent (and, with --require-final,
releasable); 1 = validation or eligibility failures; 2 = usage / parse
errors. Called by scripts/release_gate.sh - it is the single source of
truth, no hand-counted totals allowed.

--report-json <path> writes a machine-readable report (mode, freeze sha,
status counts, releasable flag, non-releasable AC list, problems) that
scripts/release_gate.sh embeds into gate-summary.json so the release
summary records coverage AND eligibility instead of a bare PASS.
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

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DEFAULT = "TODO/acceptance-profile.yaml"
SCHEMA_DEFAULT = "TODO/evidence-manifest.schema.json"

VALID_STATUSES = {
    "not-run",
    "running",
    "pass",
    "fail",
    "blocked",
    "superseded",
    "not-applicable-approved",
}

RELEASABLE_STATUSES = {"pass", "not-applicable-approved"}

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


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def git_head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD")


def git_is_ancestor(root: Path, sha: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True,
    )
    return proc.returncode == 0


def git_changed_paths_since(root: Path, sha: str) -> list[str]:
    if sha == git_head(root):
        return []
    return git(root, "diff", "--name-only", sha, "HEAD").splitlines()


def load_profile(root: Path, path: Path) -> dict:
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


def collect_manifests(
    root: Path, tasks: set[str]
) -> list[tuple[Path, str, str, str]]:
    """Return [(manifest_path, task_dir, sha_dir, ac_dir)]."""
    found: list[tuple[Path, str, str, str]] = []
    base = root / "tmp" / "acceptance"
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
                    found.append(
                        (manifest, task_dir.name, sha_dir.name, ac_dir.name)
                    )
    return found


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Minimal embedded JSON Schema validator (draft 2020-12 subset).
#
# The evidence manifest schema uses: type (incl. unions), const, enum,
# pattern, minLength, minItems, uniqueItems, required, properties,
# additionalProperties (false / schema), items, allOf, if/then/else, not,
# $ref / $defs. This subset checker executes those constraints directly so
# the validator does not depend on an external jsonschema install (the gate
# runs under the system python3).
# ---------------------------------------------------------------------------


def _type_ok(instance: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False


def schema_matches(instance: object, schema: dict, defs: dict) -> bool:
    """True when ``instance`` satisfies ``schema`` (violation-free)."""
    return not _schema_check(instance, schema, defs, "")


def _schema_check(
    instance: object, schema: dict, defs: dict, path: str
) -> list[str]:
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            return _schema_check(instance, defs[ref[len("#/$defs/"):]], defs, path)
        problems.append(f"{path}: unsupported $ref {ref}")
        return problems

    expected = schema.get("type")
    if expected:
        choices = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, choice) for choice in choices):
            problems.append(
                f"{path}: expected type {expected}, got {type(instance).__name__}"
            )
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: value not in enum")
    if isinstance(instance, str):
        if "pattern" in schema and not re.fullmatch(schema["pattern"], instance):
            problems.append(f"{path}: does not match pattern {schema['pattern']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            problems.append(f"{path}: shorter than minLength {schema['minLength']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            problems.append(f"{path}: fewer than minItems {schema['minItems']}")
        if schema.get("uniqueItems") and len(
            {json.dumps(x, sort_keys=True, default=str) for x in instance}
        ) != len(instance):
            problems.append(f"{path}: uniqueItems violated")
        if "items" in schema and isinstance(schema["items"], dict):
            for idx, item in enumerate(instance):
                problems.extend(
                    _schema_check(item, schema["items"], defs, f"{path}[{idx}]")
                )
    if isinstance(instance, dict):
        if "required" in schema:
            for key in schema["required"]:
                if key not in instance:
                    problems.append(f"{path}: missing required field {key!r}")
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                problems.extend(
                    _schema_check(value, props[key], defs, f"{path}.{key}")
                )
            elif additional is False:
                problems.append(f"{path}: unknown field {key!r}")
            elif isinstance(additional, dict):
                problems.extend(
                    _schema_check(value, additional, defs, f"{path}.{key}")
                )
    if "allOf" in schema:
        for sub in schema["allOf"]:
            if isinstance(sub, dict):
                problems.extend(_schema_check(instance, sub, defs, path))
    if "if" in schema and isinstance(schema["if"], dict):
        branch = "then" if schema_matches(instance, schema["if"], defs) else "else"
        if branch in schema and isinstance(schema[branch], dict):
            problems.extend(_schema_check(instance, schema[branch], defs, path))
    if "not" in schema and isinstance(schema["not"], dict):
        if schema_matches(instance, schema["not"], defs):
            problems.append(f"{path}: violates 'not' constraint")
    return problems


def load_schema(root: Path, path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        schema = json.load(fh)
    if not isinstance(schema, dict):
        raise SystemExit(f"schema {path} must be a JSON object")
    return schema


def check_path_consistency(
    manifest: Path, task_dir: str, sha_dir: str, ac_dir: str, root: Path
) -> list[str]:
    """S2-01: task_id/ac_id/implementation_sha must agree with the directory
    path (tmp/acceptance/<task_id>/<implementation_sha>/<ac_id>/).

    Runs for EVERY collected manifest - including manifests whose directory
    names fall outside the expected set, so a misplaced manifest can never
    hide behind a "no current evidence" message.
    """
    rel = str(manifest.relative_to(root))
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"{rel}: unreadable/invalid JSON: {exc}"]
    if not isinstance(data, dict):
        return [f"{rel}: manifest must be a JSON object"]
    problems: list[str] = []
    if data.get("task_id") != task_dir:
        problems.append(
            f"{rel}: task_id {data.get('task_id')!r} does not match directory {task_dir!r}"
        )
    if data.get("ac_id") != ac_dir:
        problems.append(
            f"{rel}: ac_id {data.get('ac_id')!r} does not match directory {ac_dir!r}"
        )
    if data.get("implementation_sha") != sha_dir:
        problems.append(
            f"{rel}: implementation_sha {data.get('implementation_sha')!r} does not "
            f"match directory {sha_dir!r}"
        )
    return problems


def validate_manifest(
    manifest: Path,
    *,
    schema: dict,
    freeze_sha: str,
    verify_artifacts: bool,
    root: Path,
) -> list[str]:
    problems: list[str] = []
    rel = str(manifest.relative_to(root))
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

    # S2-01: the full evidence-manifest.schema.json is executed directly,
    # including additionalProperties / conditional (if-then) constraints.
    problems.extend(_schema_check(data, schema, schema.get("$defs", {}), rel))

    if data["status"] not in VALID_STATUSES:
        problems.append(f"{rel}: unknown status {data['status']!r}")
    for oid_field in ("baseline_sha", "implementation_sha"):
        if not GIT_OID.fullmatch(str(data[oid_field])):
            problems.append(f"{rel}: {oid_field} is not a git OID")
    if not SHA256.fullmatch(str(data["environment_digest"])):
        problems.append(f"{rel}: environment_digest must be sha256 hex")
    started = parse_ts(str(data["started_at"]))
    finished = parse_ts(str(data["finished_at"]))
    for ts_field, parsed in (("started_at", started), ("finished_at", finished)):
        if parsed is None:
            problems.append(f"{rel}: {ts_field} is not a valid timestamp")
    # S2-01: time ordering - finished_at may never precede started_at.
    if started is not None and finished is not None and finished < started:
        problems.append(
            f"{rel}: finished_at {data['finished_at']} precedes "
            f"started_at {data['started_at']}"
        )
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
        # S3-05: artifacts must live inside the repository evidence scope -
        # either the normative documents pinned in the profile, or the
        # evidence tree (tmp/acceptance/**). Absolute paths, ../ escapes
        # beyond the repo root, and symlinks are all rejected.
        evidence_root = (root / "tmp" / "acceptance").resolve()
        repo_root = root.resolve()
        normative_docs = {"TODO/acceptance-profile.yaml", "SPEC/contracts/run.md"}
        for artifact in data["artifacts"]:
            if not isinstance(artifact, dict):
                problems.append(f"{rel}: artifact entry is not an object")
                continue
            raw_path = str(artifact.get("path") or "")
            if raw_path.startswith("/") or raw_path.startswith("~"):
                problems.append(f"{rel}: artifact {raw_path} must be a relative path")
                continue
            candidate = root / raw_path
            try:
                resolved = candidate.resolve()
            except OSError as exc:
                problems.append(f"{rel}: artifact {raw_path} unresolvable: {exc}")
                continue
            if candidate.is_symlink() or resolved.is_symlink():
                problems.append(f"{rel}: artifact {raw_path} must not be a symlink")
                continue
            if not resolved.is_relative_to(repo_root):
                problems.append(f"{rel}: artifact {raw_path} escapes the repository")
                continue
            in_evidence_tree = resolved.is_relative_to(evidence_root)
            if not in_evidence_tree and raw_path not in normative_docs:
                problems.append(
                    f"{rel}: artifact {raw_path} is neither a normative doc "
                    "nor inside tmp/acceptance/"
                )
                continue
            if not resolved.is_file():
                problems.append(f"{rel}: artifact {raw_path} missing at HEAD")
                continue
            if not SHA256.fullmatch(str(artifact.get("sha256") or "")):
                problems.append(f"{rel}: artifact {raw_path} sha256 malformed")
                continue
            actual = sha256_file(resolved)
            if actual != artifact["sha256"]:
                problems.append(
                    f"{rel}: artifact {raw_path} hash mismatch "
                    f"(recorded {str(artifact['sha256'])[:12]}..., actual {actual[:12]}...)"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=PROFILE_DEFAULT)
    parser.add_argument("--require-final", action="store_true")
    parser.add_argument("--task", default=None, help="validate a single task")
    parser.add_argument("--ac", default=None, help="validate a single AC (needs --task)")
    parser.add_argument(
        "--root", default=str(DEFAULT_ROOT),
        help="repository root (default: the repo containing this script)",
    )
    parser.add_argument(
        "--report-json", default=None,
        help="write a machine-readable report to this path",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"evidence validator error: {root} is not a git repository", file=sys.stderr)
        return 2

    try:
        head = git_head(root)
        profile = load_profile(root, root / args.profile)
        ac_by_task = required_ac_by_task(profile)
        schema = load_schema(root, root / SCHEMA_DEFAULT)
    except (SystemExit, ValueError, subprocess.CalledProcessError) as exc:
        print(f"evidence validator error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
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

    manifests = collect_manifests(root, set(ac_by_task))
    # S2-01: every collected manifest - regardless of directory - must have
    # task_id/ac_id/implementation_sha matching its path.
    for manifest, task_dir, sha_dir, ac_dir in manifests:
        problems.extend(
            check_path_consistency(manifest, task_dir, sha_dir, ac_dir, root)
        )
    by_ac: dict[str, list[tuple[Path, str, str, str]]] = {}
    for manifest, task_dir, sha_dir, ac_dir in manifests:
        by_ac.setdefault(ac_dir, []).append((manifest, task_dir, sha_dir, ac_dir))

    # Determine the freeze sha: the implementation_sha of every
    # non-superseded manifest must agree on exactly one commit.
    freeze_candidates: set[str] = set()
    for manifest, _task_dir, _sha_dir, _ac_dir in manifests:
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
        for manifest, _task_dir, sha_dir, _ac_dir in manifests:
            if sha_dir != freeze_sha:
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(data, dict) and data.get("status") not in {"superseded", "fail"}:
                    problems.append(
                        f"{manifest.relative_to(root)}: stale manifest (dir sha "
                        f"{sha_dir[:12]}) not marked superseded/failed"
                    )

    if args.require_final:
        if freeze_sha is None:
            problems.append("--require-final: no current (non-superseded) evidence exists")
        else:
            if freeze_sha != head and not git_is_ancestor(root, freeze_sha):
                problems.append(
                    f"--require-final: freeze sha {freeze_sha[:12]} is not HEAD "
                    "or an ancestor of HEAD"
                )
            changed = git_changed_paths_since(root, freeze_sha)
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

    status_counts: dict[str, int] = {status: 0 for status in VALID_STATUSES}
    not_releasable: list[dict[str, str]] = []

    for ac, task in sorted(expected.items()):
        if freeze_sha is None:
            problems.append(
                f"AC {ac} (task {task}): no current evidence (no freeze sha)"
            )
            continue
        # S3-05: current evidence must sit under the REGISTRY task directory
        # (tmp/acceptance/<registry-task>/<freeze>/<ac>/). Evidence placed in
        # a different-but-valid task directory is misplaced and fails.
        current = [
            item for item in by_ac.get(ac, [])
            if item[2] == freeze_sha and item[3] == ac
        ]
        misplaced = [
            item for item in by_ac.get(ac, [])
            if item[2] == freeze_sha and item[3] == ac and item[1] != task
        ]
        if misplaced:
            for manifest, task_dir, sha_dir, ac_dir in misplaced:
                problems.append(
                    f"AC {ac} belongs to task {task} but current evidence "
                    f"sits under tmp/acceptance/{task_dir}/... (misplaced)"
                )
        if not current:
            problems.append(
                f"AC {ac} (task {task}): no current evidence at "
                f"tmp/acceptance/{task}/{freeze_sha[:12]}.../"
            )
            continue
        if len(current) > 1:
            problems.append(f"AC {ac}: duplicate current manifests: {current}")
        for manifest, task_dir, sha_dir, ac_dir in current:
            problems.extend(
                validate_manifest(
                    manifest,
                    schema=schema,
                    freeze_sha=freeze_sha,
                    verify_artifacts=True,
                    root=root,
                )
            )
        # every other manifest for this AC is historical: structure only
        for manifest, task_dir, sha_dir, ac_dir in by_ac.get(ac, []):
            if sha_dir == freeze_sha:
                continue
            problems.extend(
                validate_manifest(
                    manifest,
                    schema=schema,
                    freeze_sha=freeze_sha,
                    verify_artifacts=False,
                    root=root,
                )
            )
        # S2-01 eligibility accounting: only the current manifest decides.
        current_manifest = current[0][0]
        try:
            current_data = json.loads(current_manifest.read_text(encoding="utf-8"))
            current_status = current_data.get("status")
        except (json.JSONDecodeError, OSError):
            current_status = "fail"
        if current_status in status_counts:
            status_counts[current_status] += 1
        else:
            status_counts[current_status] = status_counts.get(current_status, 0) + 1
        if args.require_final and current_status not in RELEASABLE_STATUSES:
            not_releasable.append(
                {"ac_id": ac, "task_id": task, "status": current_status}
            )

    if args.ac:
        if args.ac not in expected or problems:
            return 1
        current = [
            item for item in by_ac.get(args.ac, []) if item[2] == freeze_sha
        ]
        if not current:
            return 1
        data = json.loads(current[0][0].read_text(encoding="utf-8"))
        return 0 if data["status"] == "pass" else 1

    total = len(expected)
    releasable = not problems and not not_releasable

    report = {
        "mode": "final" if args.require_final else "structure",
        "profile": args.profile,
        "head": head,
        "freeze_sha": freeze_sha,
        "required_ac_total": total,
        "status_counts": status_counts,
        "releasable": releasable if args.require_final else None,
        "not_releasable": not_releasable if args.require_final else [],
        "problems": problems,
        "exit_code": 0 if releasable else 1,
    }
    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # S2-01: every run prints the per-status counts, never just "OK".
    status_line = " ".join(
        f"{status}={status_counts.get(status, 0)}" for status in VALID_STATUSES
    )
    if problems:
        print(
            f"evidence validation FAILED ({len(problems)} problem(s), "
            f"{total} required AC(s) checked, freeze sha "
            f"{freeze_sha[:12] if freeze_sha else 'NONE'}):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"evidence status counts: {status_line}", file=sys.stderr)
        if args.require_final and not_releasable:
            print(
                f"evidence NOT RELEASABLE: {len(not_releasable)} required AC(s) "
                "not in a releasable state (pass / unexpired "
                "not-applicable-approved):",
                file=sys.stderr,
            )
            for item in not_releasable:
                print(
                    f"  - {item['ac_id']} (task {item['task_id']}): {item['status']}",
                    file=sys.stderr,
                )
        return 1
    if args.require_final and not_releasable:
        print(
            f"evidence NOT RELEASABLE: {len(not_releasable)} required AC(s) not "
            "in a releasable state (pass / unexpired not-applicable-approved):",
            file=sys.stderr,
        )
        for item in not_releasable:
            print(
                f"  - {item['ac_id']} (task {item['task_id']}): {item['status']}",
                file=sys.stderr,
            )
        print(f"evidence status counts: {status_line}", file=sys.stderr)
        return 1
    mode_word = "OK (releasable)" if args.require_final else "OK (structure only)"
    print(
        f"evidence validation {mode_word}: {total} required AC(s) covered by "
        f"unique current manifests at freeze sha {freeze_sha[:12]}; "
        f"status counts: {status_line}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
