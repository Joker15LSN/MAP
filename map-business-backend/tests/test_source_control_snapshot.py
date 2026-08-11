"""R5-P2-02 acceptance: the shared NUL-safe source-control snapshot.

The fifth-round review proved both porcelain parsers broken:

- the E2E runner ``.strip()``-ed the whole output then sliced ``[3:]``,
  turning ``e2e/browser_e2e.py`` into ``2e/browser_e2e.py``;
- the gate parsed default porcelain without ``core.quotepath=false``, so
  Chinese docs paths arrived as quoted octal escapes and the docs-only
  rule never matched.

These tests run against REAL temporary git repositories and assert that
parsed paths are BYTE-IDENTICAL to the true repo-relative paths for the
seven mandated shapes: first-line tracked modification, plain untracked,
paths with spaces, Chinese Markdown, rename, deletion, and the same file
staged AND unstaged. They also pin the docs/product classification and
the ``--require-clean-product`` exit contract.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "source_control.py"

_spec = importlib.util.spec_from_file_location("map_source_control_under_test", _SCRIPT)
source_control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source_control)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A real committed baseline repository (identity scoped locally)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "source-control-test@example.com")
    _git(repo, "config", "user.name", "source-control-test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _snapshot_paths(repo: Path) -> dict:
    snap = source_control.snapshot(repo)
    return snap


def test_first_line_tracked_modification_is_not_truncated(git_repo: Path) -> None:
    """Regression for ``2e/browser_e2e.py``: even the FIRST porcelain line
    (previously eaten by the whole-output ``.strip()``) must keep every
    byte of the path."""
    (git_repo / "e2e").mkdir()
    (git_repo / "e2e" / "browser_e2e.py").write_text("print('v1')\n")
    _commit(git_repo, "baseline")
    (git_repo / "e2e" / "browser_e2e.py").write_text("print('v2')\n")

    snap = _snapshot_paths(git_repo)
    assert snap["dirty"] is True
    assert snap["dirty_files"] == ["e2e/browser_e2e.py"]  # NOT "2e/browser_e2e.py"
    assert snap["dirty_product"] == ["e2e/browser_e2e.py"]
    assert snap["docs_only_dirty"] is False


def test_plain_untracked_file(git_repo: Path) -> None:
    (git_repo / "tracked.txt").write_text("base\n")
    _commit(git_repo, "baseline")
    (git_repo / "brand_new.py").write_text("x = 1\n")

    snap = _snapshot_paths(git_repo)
    assert "brand_new.py" in snap["dirty_files"]
    entry = next(e for e in snap["entries"] if e["path"] == "brand_new.py")
    assert entry["xy"] == "??"
    # untracked content evidence (R5-P2-02 content manifest)
    manifest = {m["path"]: m["sha256"] for m in snap["untracked_manifest"]}
    assert "brand_new.py" in manifest and len(manifest["brand_new.py"]) == 64


def test_path_with_spaces(git_repo: Path) -> None:
    # A TRACKED file inside a spacey directory (git coalesces untracked
    # directories into a single "dir/" entry; tracked paths are exact).
    (git_repo / "dir with spaces").mkdir()
    (git_repo / "dir with spaces" / "file name.py").write_text("y = 1\n")
    _commit(git_repo, "baseline")
    (git_repo / "dir with spaces" / "file name.py").write_text("y = 2\n")

    snap = _snapshot_paths(git_repo)
    assert snap["dirty_files"] == ["dir with spaces/file name.py"]
    # and a spacey UNTRACKED file at the top level stays byte-exact too
    (git_repo / "another new file.txt").write_text("n\n")
    snap = _snapshot_paths(git_repo)
    assert "another new file.txt" in snap["dirty_files"]


def test_chinese_markdown_is_docs_and_byte_exact(git_repo: Path) -> None:
    """Regression for quoted octal escapes: a Chinese docs path must be
    parsed byte-exact AND classified as docs (``docs_only_dirty=True``)."""
    (git_repo / "TODO").mkdir()
    docs_path = "TODO/下一阶段产品规划_第五轮整改.md"
    (git_repo / docs_path).write_text("# v1\n", encoding="utf-8")
    _commit(git_repo, "baseline")
    (git_repo / docs_path).write_text("# v2\n", encoding="utf-8")

    snap = _snapshot_paths(git_repo)
    assert snap["dirty_files"] == [docs_path]
    assert not any("\\" in path for path in snap["dirty_files"])  # no octal escapes
    assert snap["docs_only_dirty"] is True
    assert snap["dirty_product"] == []


def test_rename_reports_both_paths(git_repo: Path) -> None:
    (git_repo / "old_name.py").write_text("content\n")
    _commit(git_repo, "baseline")
    _git(git_repo, "mv", "old_name.py", "new_name.py")

    snap = _snapshot_paths(git_repo)
    renamed = [e for e in snap["entries"] if e["xy"].startswith("R")]
    assert renamed, f"no rename entry in {snap['entries']!r}"
    assert renamed[0]["path"] == "new_name.py"
    assert renamed[0]["orig_path"] == "old_name.py"
    assert "new_name.py" in snap["dirty_files"]
    assert "old_name.py" not in snap["dirty_files"]


def test_deletion(git_repo: Path) -> None:
    (git_repo / "doomed.py").write_text("gone soon\n")
    (git_repo / "keeper.txt").write_text("stays\n")
    _commit(git_repo, "baseline")
    (git_repo / "doomed.py").unlink()

    snap = _snapshot_paths(git_repo)
    entry = next(e for e in snap["entries"] if e["path"] == "doomed.py")
    assert entry["xy"] == " D"
    assert snap["dirty_product"] == ["doomed.py"]


def test_staged_and_unstaged_same_file(git_repo: Path) -> None:
    (git_repo / "mixed.py").write_text("line1\n")
    _commit(git_repo, "baseline")
    (git_repo / "mixed.py").write_text("line1\nline2\n")
    _git(git_repo, "add", "mixed.py")
    (git_repo / "mixed.py").write_text("line1\nline2\nline3\n")

    snap = _snapshot_paths(git_repo)
    entry = next(e for e in snap["entries"] if e["path"] == "mixed.py")
    assert entry["xy"] == "MM"
    # working-tree content evidence exists for the dirty tree
    assert len(snap["diff_head_sha256"]) == 64


def test_mixed_dirty_classifies_product_dirty(git_repo: Path) -> None:
    (git_repo / "TODO").mkdir()
    (git_repo / "TODO" / "notes.md").write_text("a\n")
    (git_repo / "app.py").write_text("x = 1\n")
    _commit(git_repo, "baseline")
    (git_repo / "TODO" / "notes.md").write_text("b\n")
    (git_repo / "app.py").write_text("x = 2\n")

    snap = _snapshot_paths(git_repo)
    assert sorted(snap["dirty_files"]) == ["TODO/notes.md", "app.py"]
    assert snap["dirty_product"] == ["app.py"]
    assert snap["docs_only_dirty"] is False


def test_clean_tree_has_no_dirty_fields(git_repo: Path) -> None:
    (git_repo / "clean.txt").write_text("ok\n")
    _commit(git_repo, "baseline")

    snap = _snapshot_paths(git_repo)
    assert snap["dirty"] is False
    assert snap["dirty_files"] == []
    assert snap["dirty_product"] == []
    assert snap["docs_only_dirty"] is False
    assert "diff_head_sha256" not in snap  # content evidence only when dirty
    assert snap["git_sha"] and snap["git_tree"] and snap["branch"]


def test_cli_require_clean_product_exit_codes(git_repo: Path) -> None:
    """Final-mode contract: docs-only dirtiness exits 0; any dirty PRODUCT
    file exits non-zero (2) BEFORE any test may run."""
    (git_repo / "TODO").mkdir()
    (git_repo / "TODO" / "计划.md").write_text("a\n", encoding="utf-8")
    (git_repo / "product.py").write_text("p = 1\n")
    _commit(git_repo, "baseline")

    # docs-only dirty -> allowed
    (git_repo / "TODO" / "计划.md").write_text("b\n", encoding="utf-8")
    rc = source_control.main(
        ["--repo", str(git_repo), "--json", "--require-clean-product"]
    )
    assert rc == 0

    # product dirty -> refused
    (git_repo / "product.py").write_text("p = 2\n")
    rc = source_control.main(
        ["--repo", str(git_repo), "--json", "--require-clean-product"]
    )
    assert rc == 2


def test_parser_rejects_malformed_and_incomplete_records() -> None:
    with pytest.raises(ValueError, match="malformed"):
        source_control.parse_porcelain_z(b"M\x00")
    with pytest.raises(ValueError, match="origin path"):
        source_control.parse_porcelain_z(b"R  new.py\x00")  # missing origin record
    assert source_control.parse_porcelain_z(b"") == []
