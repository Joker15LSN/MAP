"""R7-P2-02 acceptance: release-gate whitespace checks must cover BOTH the
working tree AND the committed range.

The seventh-round review proved the old gate's ``git diff --check`` a
false green: with a clean worktree it only inspects uncommitted drift,
so whitespace defects ALREADY COMMITTED (a blank line at EOF in the
quality record) passed every gate run, and ``GATE_BASELINE_SHA`` was
recorded but never drove a check.

These tests pin the replacement contract of ``scripts/gate_diff_check.sh``
against REAL temporary git repositories:

- ``worktree`` mode: ``git diff --check`` on uncommitted drift;
- ``committed <baseline>`` mode: ``git diff --check <baseline> HEAD``;
- baseline validation is fail-closed: missing, unresolvable or
  non-ancestor baselines exit 3 — never a silently empty diff;
- ``RELEASE_GATE_FINAL=1`` refuses to run without a valid baseline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_DIFF_CHECK = REPO_ROOT / "scripts" / "gate_diff_check.sh"
_RELEASE_GATE = REPO_ROOT / "scripts" / "release_gate.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A real committed baseline repository (identity scoped locally)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "gate-diff-test@example.com")
    _git(repo, "config", "user.name", "gate-diff-test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "clean.txt").write_text("baseline\n")
    _commit(repo, "baseline")
    return repo


def _run_check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_DIFF_CHECK), *args],
        cwd=str(repo), capture_output=True, text=True,
    )


def test_worktree_check_passes_on_clean_tree(git_repo: Path) -> None:
    assert _run_check(git_repo, "worktree").returncode == 0


def test_committed_range_detects_defects_on_clean_worktree(git_repo: Path) -> None:
    """R7-P2-02 failure reproduction: after the baseline, commit a file
    with trailing whitespace and a blank line at EOF, then keep the
    worktree CLEAN. The worktree check stays 0 (the old gate's false
    green), but the committed-range step must be non-zero and fail the
    final gate."""
    baseline = _git_sha(git_repo)
    (git_repo / "defect.txt").write_text("trailing  \ncontent\n\n")
    _commit(git_repo, "introduce whitespace defects")

    worktree = _run_check(git_repo, "worktree")
    assert worktree.returncode == 0  # clean worktree hides nothing anymore
    committed = _run_check(git_repo, "committed", baseline)
    assert committed.returncode != 0
    assert "defect.txt" in (committed.stdout + committed.stderr)


def test_committed_range_recovers_after_fix_commit(git_repo: Path) -> None:
    """Fixing the defect in a follow-up commit restores the range check."""
    baseline = _git_sha(git_repo)
    (git_repo / "defect.txt").write_text("trailing  \ncontent\n\n")
    _commit(git_repo, "introduce whitespace defects")
    assert _run_check(git_repo, "committed", baseline).returncode != 0

    (git_repo / "defect.txt").write_text("trailing\ncontent\n")
    _commit(git_repo, "fix whitespace defects")
    assert _run_check(git_repo, "committed", baseline).returncode == 0


def test_missing_baseline_fails_closed(git_repo: Path) -> None:
    for mode in ("validate", "committed"):
        result = _run_check(git_repo, mode)
        assert result.returncode == 3, result.stderr
        assert "baseline" in result.stderr.lower()


def test_unresolvable_baseline_fails_closed(git_repo: Path) -> None:
    result = _run_check(git_repo, "committed", "deadbeef" * 5)
    assert result.returncode == 3
    assert "does not resolve" in result.stderr


def test_non_ancestor_baseline_fails_closed(git_repo: Path) -> None:
    """A baseline that is not an ancestor of HEAD would produce a
    misleading diff; the direction is pinned via merge-base."""
    _git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "side.txt").write_text("side\n")
    _commit(git_repo, "side branch commit")
    side_sha = _git_sha(git_repo)
    _git(git_repo, "checkout", "-q", "-")

    result = _run_check(git_repo, "committed", side_sha)
    assert result.returncode == 3
    assert "not an ancestor" in result.stderr


def test_unknown_mode_fails_closed(git_repo: Path) -> None:
    assert _run_check(git_repo, "bogus").returncode == 3


def _git_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# ---- RELEASE_GATE_FINAL=1 must refuse missing/invalid baseline ----------
# These run the REAL gate script on the real repository, but it exits at
# the baseline validation BEFORE any heavy step; logs go to a throwaway
# directory so the evidence artifacts under tmp/gate-logs stay untouched.


def _run_final_gate(tmp_path: Path, baseline: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "RELEASE_GATE_FINAL": "1", "GATE_LOG_DIR": str(tmp_path / "logs")}
    if baseline is None:
        env.pop("GATE_BASELINE_SHA", None)
    else:
        env["GATE_BASELINE_SHA"] = baseline
    return subprocess.run(
        ["bash", str(_RELEASE_GATE)],
        env=env, capture_output=True, text=True, timeout=180,
    )


def test_final_gate_without_baseline_refused(tmp_path: Path) -> None:
    result = _run_final_gate(tmp_path, None)
    assert result.returncode != 0
    assert "GATE_BASELINE_SHA" in (result.stdout + result.stderr)


def test_final_gate_with_unresolvable_baseline_refused(tmp_path: Path) -> None:
    result = _run_final_gate(tmp_path, "deadbeef" * 5)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "GATE_BASELINE_SHA" in combined or "does not resolve" in combined
