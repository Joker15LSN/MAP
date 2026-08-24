"""S7-01 acceptance: the protected-CI final sequence no longer self-blocks.

Seventh-round P0 reproduction, pinned verbatim:

- CI attests pass evidence at HEAD (the evidence re-freeze commit), which
  superseded the entire tracked evidence set and regenerated it under a
  new sha; the final gate then saw tmp/acceptance/** as dirty PRODUCT and
  exited before its first gate step. ``RELEASE_GATE_FINAL=1`` could never
  turn green, with or without the signing secret.

The fix has three legs, all exercised HERE in one real sequence on a
synthetic git repository:

1. the protected workflow injects the IMPLEMENTATION commit
   (MAP_EVIDENCE_IMPLEMENTATION_SHA, resolved by walking back from HEAD
   to the first non-evidence-only commit);
2. the evidence generator freezes there and re-attests pass manifests
   IN PLACE instead of superseding/regenerating everything;
3. scripts/source_control.py classifies tmp/acceptance/** as EVIDENCE,
   so the evidence-only worktree rewrite passes --require-clean-product
   and the REAL release_gate.sh startup check (RELEASE_GATE_FINAL=1).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(script: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_generator = _load_module(
    REPO_ROOT / "scripts" / "generate_acceptance_evidence.py",
    "generate_acceptance_evidence_under_test",
)
_source_control = _load_module(
    REPO_ROOT / "scripts" / "source_control.py",
    "source_control_under_test",
)
_signing = _load_module(
    REPO_ROOT / "scripts" / "evidence_signing.py",
    "evidence_signing_under_test",
)
_resolver = _load_module(
    REPO_ROOT / "scripts" / "resolve_evidence_implementation_sha.py",
    "resolve_evidence_implementation_sha_under_test",
)

_PROFILE = """schema_version: "1.1.0"
profile_id: "synthetic-ci-sequence-test"
task_registry:
  GLOBAL:
    depends_on: []
    acceptance_ids: [AC-ONE, AC-BLOCKED]
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _build_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    """Implementation commit + evidence-only commit on top (CI HEAD)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci-sequence-test@example.com")
    _git(repo, "config", "user.name", "ci-sequence-test")
    _git(repo, "config", "commit.gpgsign", "false")
    # Gate logs are runtime artifacts, never tracked dirtiness.
    (repo / ".gitignore").write_text("tmp/gate-logs/\n", encoding="utf-8")

    (repo / "TODO").mkdir()
    (repo / "TODO" / "acceptance-profile.yaml").write_text(_PROFILE, encoding="utf-8")
    (repo / "SPEC").mkdir()
    (repo / "SPEC" / "contracts").mkdir(parents=True)
    (repo / "SPEC" / "contracts" / "run.md").write_text("# run\n", encoding="utf-8")
    (repo / "app.py").write_text("print('product')\n", encoding="utf-8")
    # The REAL gate scripts are part of the implementation commit, exactly
    # like the real repository (the synthetic repo is the repo under test).
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    for name in ("release_gate.sh", "gate_diff_check.sh", "source_control.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts_dir / name)
    implementation_sha = _commit(repo, "implementation freeze")

    # The evidence re-freeze commit: touches ONLY tmp/acceptance/**.
    manifest_dir = (
        repo / "tmp" / "acceptance" / "GLOBAL" / implementation_sha / "AC-ONE"
    )
    manifest_dir.mkdir(parents=True)
    manifest = {
        "schema_version": "1.2.0",
        "task_id": "GLOBAL",
        "ac_id": "AC-ONE",
        "status": "pass",
        "baseline_sha": implementation_sha,
        "implementation_sha": implementation_sha,
        "environment_digest": "0" * 64,
        "started_at": "2026-08-15T00:00:00Z",
        "finished_at": "2026-08-15T00:00:01Z",
        "command": "pytest synthetic-pass",
        "exit_code": 0,
        "artifacts": [],
        "assertions": [
            {"name": "synthetic", "expected": "pass", "actual": "pass",
             "result": "pass"}
        ],
        "finding_ids": [],
        "waiver_id": None,
        "waiver_owner": None,
        "waiver_expires_at": None,
        "not_applicable_reason": None,
        "blocked_reason": None,
        "blocker_owner": None,
        "superseded_by": None,
        "superseded_reason": None,
        "producer": {"agent": "synthetic", "version": "1"},
        "attestation": None,
    }
    (manifest_dir / "evidence-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    blocked_dir = (
        repo / "tmp" / "acceptance" / "GLOBAL" / implementation_sha / "AC-BLOCKED"
    )
    blocked_dir.mkdir(parents=True)
    blocked_manifest = {
        **manifest,
        "ac_id": "AC-BLOCKED",
        "status": "blocked",
        "exit_code": 1,
        "blocked_reason": "synthetic blocker",
    }
    (blocked_dir / "evidence-manifest.json").write_text(
        json.dumps(blocked_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # A historical superseded manifest (previous freeze) must NOT be
    # rewritten by a CI re-attest at the new implementation sha - that was
    # the 1599-file rewrite half of the seventh-round P0 reproduction.
    old_sha = "0" * 40
    old_dir = repo / "tmp" / "acceptance" / "GLOBAL" / old_sha / "AC-ONE"
    old_dir.mkdir(parents=True)
    old_manifest = {
        "schema_version": "1.2.0",
        "task_id": "GLOBAL",
        "ac_id": "AC-ONE",
        "status": "superseded",
        "superseded_by": (
            f"tmp/acceptance/GLOBAL/{implementation_sha}/AC-ONE/"
            "evidence-manifest.json"
        ),
        "superseded_reason": "superseded by evidence at freeze sha "
        + implementation_sha,
        "attestation": None,
    }
    old_path = old_dir / "evidence-manifest.json"
    old_path.write_text(
        json.dumps(old_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _commit(repo, "evidence(S7): re-freeze acceptance evidence")
    return repo, implementation_sha, manifest_dir / "evidence-manifest.json", old_path


@pytest.fixture()
def ci_repo(tmp_path: Path):
    return _build_repo(tmp_path)


def test_ci_final_sequence(ci_repo, monkeypatch) -> None:
    repo, implementation_sha, manifest_path, old_path = ci_repo
    old_bytes_before = old_path.read_bytes()

    # The workflow resolver must find the implementation commit, never the
    # evidence commit that is HEAD.
    assert _resolver.resolve_implementation_sha(repo) == implementation_sha

    # Leg 2: run the REAL evidence generator with the CI signing context
    # and the externally injected implementation sha.
    secret, _public = _signing.generate_keypair()
    monkeypatch.setenv("MAP_EVIDENCE_CI", "1")
    monkeypatch.setenv("EVIDENCE_SIGNING_KEY", secret)
    monkeypatch.setenv("MAP_EVIDENCE_REPOSITORY", "owner/repo")
    monkeypatch.setenv("MAP_EVIDENCE_GIT_REF", "refs/heads/main")
    monkeypatch.setenv("MAP_EVIDENCE_RUN_ID", "run-7a50d808")
    monkeypatch.setenv("MAP_EVIDENCE_RUN_ATTEMPT", "1")
    monkeypatch.setenv("MAP_EVIDENCE_IMPLEMENTATION_SHA", implementation_sha)

    original_root = _generator.ROOT
    _generator.ROOT = repo
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = _generator.main(["--profile", "TODO/acceptance-profile.yaml"])
    finally:
        _generator.ROOT = original_root
    assert rc == 0, stderr.getvalue()
    assert implementation_sha[:12] in stdout.getvalue()
    assert "1 re-attested" in stdout.getvalue()
    assert "1 already present" in stdout.getvalue()
    # The re-attested manifest is a worktree modification under
    # tmp/acceptance/** and must NOT have created a second freeze dir.
    attested = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert attested["status"] == "pass"
    assert attested["attestation"] is not None
    assert attested["implementation_sha"] == implementation_sha
    # The historical superseded record is byte-identical: no 1599-file
    # rewrite on a protected-CI re-attest.
    assert old_path.read_bytes() == old_bytes_before

    # Leg 3a: the ONE source-control classifier now treats the
    # evidence-only rewrite as tolerated for a clean product tree.
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = _source_control.main(
            ["--repo", str(repo), "--json", "--require-clean-product"]
        )
    assert rc == 0, out.getvalue()
    snapshot = json.loads(out.getvalue())
    assert snapshot["dirty_product"] == []
    assert snapshot["dirty_evidence"], snapshot
    assert snapshot["evidence_only_dirty"] is True
    assert manifest_path.relative_to(repo).as_posix() in snapshot["dirty_evidence"]

    # Leg 3b: the REAL release_gate.sh startup check (final mode, baseline
    # validation + clean-product refusal) passes on this exact worktree.
    env = {
        **os.environ,
        "RELEASE_GATE_FINAL": "1",
        "GATE_BASELINE_SHA": implementation_sha,
        "GATE_STARTUP_CHECK_ONLY": "1",
    }
    proc = subprocess.run(
        ["bash", "scripts/release_gate.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "STARTUP CHECK PASSED" in proc.stdout
    assert "refuses dirty product code" not in proc.stdout


def test_product_dirt_still_blocks_startup_check(ci_repo) -> None:
    """The evidence exemption must not widen into tolerating real product
    dirtiness - the same startup check still fails on product code."""
    repo, implementation_sha, _manifest_path, _old_path = ci_repo
    (repo / "app.py").write_text("print('dirty product')\n", encoding="utf-8")

    env = {
        **os.environ,
        "RELEASE_GATE_FINAL": "1",
        "GATE_BASELINE_SHA": implementation_sha,
        "GATE_STARTUP_CHECK_ONLY": "1",
    }
    proc = subprocess.run(
        ["bash", "scripts/release_gate.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1
    assert "refuses dirty product code" in proc.stdout


def test_generator_rejects_unknown_injected_implementation_sha(
    ci_repo, monkeypatch
) -> None:
    """Fail-closed: an injected implementation sha that does not resolve
    (or is not HEAD/ancestor of HEAD) must abort, never guess."""
    repo, _implementation_sha, _manifest_path, _old_path = ci_repo
    monkeypatch.setenv("MAP_EVIDENCE_IMPLEMENTATION_SHA", "deadbeef" * 5)
    original_root = _generator.ROOT
    _generator.ROOT = repo
    try:
        with pytest.raises(SystemExit):
            _generator.main(["--profile", "TODO/acceptance-profile.yaml"])
    finally:
        _generator.ROOT = original_root
