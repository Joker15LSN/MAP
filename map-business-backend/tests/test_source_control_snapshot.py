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

R6-P2-01 extended the classification so a staged rename (``XY="R "``)
contributes BOTH paths; R7-P2-01 completes the state space: git
porcelain rename can sit in EITHER column — ``mv app.py TODO/app.py.md
&& git add -N TODO/app.py.md`` stably yields ``XY=" R"`` in a real
repository, and the worktree-side form must classify exactly like the
index-side one. The quadrant matrix below runs every rename class in
BOTH column positions against real temporary repositories.

S8-02 narrows the acceptance-evidence exemption to committed files with
known evidence shapes. Untracked or unexpected files below
``tmp/acceptance/`` remain product dirt.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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
    # R6-P2-01: a rename is "old path deleted + new path added" — BOTH
    # paths are affected and must be visible in the dirty set.
    assert "new_name.py" in snap["dirty_files"]
    assert "old_name.py" in snap["dirty_files"]
    assert snap["affected_paths"] == ["new_name.py", "old_name.py"]


def test_r6_product_to_docs_rename_is_refused(git_repo: Path) -> None:
    """R6-P2-01 failure reproduction, pinned verbatim: staging
    ``git mv app.py TODO/app.py.md`` must NOT look docs-only — the old
    product path is being deleted, so ``--require-clean-product`` must
    exit 2 (it used to return 0)."""
    (git_repo / "app.py").write_text("product\n")
    (git_repo / "TODO").mkdir()
    _commit(git_repo, "baseline")
    _git(git_repo, "mv", "app.py", "TODO/app.py.md")

    snap = _snapshot_paths(git_repo)
    assert snap["affected_paths"] == ["TODO/app.py.md", "app.py"]
    assert snap["dirty_product"] == ["app.py"]
    assert snap["docs_only_dirty"] is False
    rc = source_control.main(
        ["--repo", str(git_repo), "--json", "--require-clean-product"]
    )
    assert rc == 2


def test_rename_four_quadrants_classification(git_repo: Path) -> None:
    """R6-P2-01 acceptance matrix: product->product, product->docs and
    docs->product renames are ALL product-dirty; only docs->docs stays
    docs-only."""
    # Distinct contents: rename pairing must stay unambiguous.
    (git_repo / "prod_a.py").write_text("alpha\n")
    (git_repo / "prod_b.py").write_text("bravo\n")
    (git_repo / "TODO").mkdir()
    (git_repo / "TODO" / "docs_a.md").write_text("delta docs\n")
    (git_repo / "TODO" / "docs_b.md").write_text("echo docs\n")
    _commit(git_repo, "baseline")

    # Three product-affecting quadrants in ONE tree.
    _git(git_repo, "mv", "prod_a.py", "prod_a2.py")          # product -> product
    _git(git_repo, "mv", "prod_b.py", "TODO/prod_b.md")       # product -> docs
    _git(git_repo, "mv", "TODO/docs_a.md", "docs_a.py")       # docs -> product

    snap = _snapshot_paths(git_repo)
    # Product-affecting paths: both sides of product->product, the DELETED
    # origin of product->docs, the NEW destination of docs->product. The
    # docs side of the two cross-boundary renames stays docs.
    assert snap["dirty_product"] == [
        "docs_a.py",
        "prod_a.py",
        "prod_a2.py",
        "prod_b.py",
    ]
    assert "TODO/prod_b.md" not in snap["dirty_product"]  # docs destination
    assert "TODO/docs_a.md" not in snap["dirty_product"]  # docs origin
    assert snap["docs_only_dirty"] is False


def test_rename_docs_to_docs_stays_docs_only(git_repo: Path) -> None:
    """The ONLY rename quadrant that may keep docs_only_dirty=true."""
    (git_repo / "TODO").mkdir()
    (git_repo / "TODO" / "old.md").write_text("d\n")
    _commit(git_repo, "baseline")
    _git(git_repo, "mv", "TODO/old.md", "TODO/new.md")

    snap = _snapshot_paths(git_repo)
    assert snap["affected_paths"] == ["TODO/new.md", "TODO/old.md"]
    assert snap["dirty_product"] == []
    assert snap["docs_only_dirty"] is True
    rc = source_control.main(
        ["--repo", str(git_repo), "--json", "--require-clean-product"]
    )
    assert rc == 0


def test_copy_classified_by_destination_only() -> None:
    """R6-P2-01: a COPY leaves its origin in place, so only the
    destination drives classification; the origin stays in the entry
    (and therefore the artifact) for audit."""
    entry = source_control.parse_porcelain_z(b"C  TODO/copy.md\x00product.py\x00")[0]
    assert entry["orig_path"] == "product.py"  # kept for audit
    assert source_control.affected_paths_for(entry) == ["TODO/copy.md"]


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


def _evidence_fixture(git_repo: Path) -> tuple[Path, Path, Path]:
    freeze_sha = "a" * 40
    evidence_dir = (
        git_repo / "tmp" / "acceptance" / "P0-TEST-01" / freeze_sha / "AC-ONE"
    )
    logs_dir = evidence_dir / "logs"
    logs_dir.mkdir(parents=True)
    log_path = logs_dir / "stdout.txt"
    log_path.write_text("original log\n", encoding="utf-8")
    custom_artifact = evidence_dir / "result.json"
    custom_artifact.write_text('{"result": "original"}\n', encoding="utf-8")
    custom_rel = custom_artifact.relative_to(git_repo).as_posix()
    manifest_path = evidence_dir / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps({"status": "pass", "artifacts": [{"path": custom_rel}]})
        + "\n",
        encoding="utf-8",
    )
    return manifest_path, log_path, custom_artifact


def test_s8_tracked_known_evidence_shapes_remain_exempt(git_repo: Path) -> None:
    """Re-attested manifests, logs, and committed manifest-referenced
    in-directory artifacts are evidence rather than product code."""
    manifest_path, log_path, custom_artifact = _evidence_fixture(git_repo)
    _commit(git_repo, "tracked evidence")

    manifest_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "artifacts": [
                    {"path": custom_artifact.relative_to(git_repo).as_posix()}
                ],
                "attestation": "new",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log_path.write_text("new log\n", encoding="utf-8")
    custom_artifact.write_text('{"result": "new"}\n', encoding="utf-8")

    snap = _snapshot_paths(git_repo)
    expected = sorted(
        path.relative_to(git_repo).as_posix()
        for path in (manifest_path, log_path, custom_artifact)
    )
    assert snap["dirty_evidence"] == expected
    assert snap["dirty_product"] == []
    assert snap["evidence_only_dirty"] is True
    assert source_control.main(
        ["--repo", str(git_repo), "--require-clean-product"]
    ) == 0


def test_s8_untracked_file_under_acceptance_is_product_dirt(git_repo: Path) -> None:
    (git_repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit(git_repo, "baseline")
    evil_path = git_repo / "tmp" / "acceptance" / "evil.py"
    evil_path.parent.mkdir(parents=True)
    evil_path.write_text("raise SystemExit('bypass')\n", encoding="utf-8")

    snap = _snapshot_paths(git_repo)
    assert snap["dirty_evidence"] == []
    assert snap["dirty_product"] == ["tmp/acceptance/evil.py"]
    assert source_control.main(
        ["--repo", str(git_repo), "--require-clean-product"]
    ) == 2


def test_s8_tracked_unexpected_acceptance_file_is_product_dirt(
    git_repo: Path,
) -> None:
    manifest_path, _log_path, _custom_artifact = _evidence_fixture(git_repo)
    unexpected = manifest_path.parent / "unexpected.bin"
    unexpected.write_bytes(b"original")
    _commit(git_repo, "evidence plus unexpected tracked file")
    unexpected.write_bytes(b"changed")

    snap = _snapshot_paths(git_repo)
    rel = unexpected.relative_to(git_repo).as_posix()
    assert snap["dirty_evidence"] == []
    assert snap["dirty_product"] == [rel]
    assert source_control.main(
        ["--repo", str(git_repo), "--require-clean-product"]
    ) == 2


def test_parser_rejects_malformed_and_incomplete_records() -> None:
    with pytest.raises(ValueError, match="malformed"):
        source_control.parse_porcelain_z(b"M\x00")
    with pytest.raises(ValueError, match="origin path"):
        source_control.parse_porcelain_z(b"R  new.py\x00")  # missing origin record
    assert source_control.parse_porcelain_z(b"") == []


# ---- R7-P2-01: rename classification must cover BOTH XY columns --------
# ``git mv`` stages the rename (``XY="R "``); a plain ``mv`` followed by
# ``git add -N`` leaves it in the worktree column (``XY=" R"``). Both are
# legal git states of the SAME operation and both must drive the
# docs/product classification with destination AND origin.


def _worktree_rename(repo: Path, src: str, dst: str) -> None:
    """Real-repo worktree-side rename: ``mv`` + intent-to-add."""
    (repo / src).rename(repo / dst)
    _git(repo, "add", "-N", dst)


def test_r7_worktree_rename_product_to_docs_refused(git_repo: Path) -> None:
    """R7-P2-01 failure reproduction, pinned verbatim from the seventh
    round: ``mv app.py TODO/app.py.md && git add -N TODO/app.py.md``
    produces ``XY=" R"``; the origin ``app.py`` is a PRODUCT path being
    deleted, so it must land in ``dirty_product`` and the final CLI must
    exit 2 (it used to report docs-only and exit 0)."""
    (git_repo / "app.py").write_text("product\n")
    (git_repo / "TODO").mkdir()
    _commit(git_repo, "baseline")
    _worktree_rename(git_repo, "app.py", "TODO/app.py.md")

    snap = _snapshot_paths(git_repo)
    entry = snap["entries"][0]
    assert entry["xy"] == " R"  # the worktree column, NOT the index one
    assert entry["orig_path"] == "app.py"
    assert snap["affected_paths"] == ["TODO/app.py.md", "app.py"]
    assert snap["dirty_product"] == ["app.py"]
    assert snap["docs_only_dirty"] is False
    rc = source_control.main(
        ["--repo", str(git_repo), "--json", "--require-clean-product"]
    )
    assert rc == 2


# (src, dst, expected dirty_product, expected CLI exit)
_R7_QUADRANTS = [
    ("prod_a.py", "prod_a2.py", ["prod_a.py", "prod_a2.py"], 2),   # prod->prod
    ("prod_b.py", "TODO/prod_b.md", ["prod_b.py"], 2),             # prod->docs
    ("TODO/docs_a.md", "docs_a.py", ["docs_a.py"], 2),             # docs->prod
    ("TODO/old.md", "TODO/new.md", [], 0),                         # docs->docs
]


@pytest.mark.parametrize("form", ["staged", "worktree"])
@pytest.mark.parametrize("src,dst,expected_product,expected_exit", _R7_QUADRANTS)
def test_r7_rename_quadrants_both_xy_columns(
    git_repo: Path, form: str, src: str, dst: str,
    expected_product: list, expected_exit: int,
) -> None:
    """R7-P2-01 acceptance matrix: four rename quadrants x BOTH column
    positions (``R `` via ``git mv``, `` R`` via ``mv`` + ``add -N``) =
    8 real-repo cases. The first three quadrants are product-dirty in
    both forms; only docs->docs may stay docs-only."""
    src_path = git_repo / src
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text(f"unique content of {src}\n")
    _commit(git_repo, "baseline")

    (git_repo / dst).parent.mkdir(parents=True, exist_ok=True)
    if form == "staged":
        _git(git_repo, "mv", src, dst)
        expected_xy = "R "
    else:
        _worktree_rename(git_repo, src, dst)
        expected_xy = " R"

    snap = _snapshot_paths(git_repo)
    renamed = [e for e in snap["entries"] if e["path"] == dst]
    assert renamed, f"no rename entry for {dst} in {snap['entries']!r}"
    assert renamed[0]["xy"] == expected_xy
    assert renamed[0]["orig_path"] == src
    # BOTH paths always affected, deduplicated and stably sorted.
    assert sorted({src, dst} & set(snap["affected_paths"])) == sorted([src, dst])
    assert snap["dirty_product"] == expected_product
    assert snap["docs_only_dirty"] == (expected_exit == 0)
    rc = source_control.main(
        ["--repo", str(git_repo), "--json", "--require-clean-product"]
    )
    assert rc == expected_exit


def test_r7_copy_worktree_column_destination_only() -> None:
    """A COPY never deletes its origin, so the worktree-column copy form
    (``XY=" C"``) classifies by destination only — the origin stays in
    the entry for audit, mirroring the index-column ``C `` semantics."""
    entry = source_control.parse_porcelain_z(b" C TODO/copy.md\x00product.py\x00")[0]
    assert entry["orig_path"] == "product.py"  # kept for audit
    assert source_control.affected_paths_for(entry) == ["TODO/copy.md"]


def test_r7_cli_and_module_classify_identically(git_repo: Path) -> None:
    """The gate consumes the CLI ``--json`` output and the E2E runner
    consumes ``snapshot()`` — on the SAME worktree-side rename repo they
    must produce byte-identical classification (no second classifier)."""
    (git_repo / "app.py").write_text("product\n")
    (git_repo / "TODO").mkdir()
    _commit(git_repo, "baseline")
    _worktree_rename(git_repo, "app.py", "TODO/app.py.md")

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = source_control.main(["--repo", str(git_repo), "--json"])
    assert rc == 0
    cli_snapshot = json.loads(buffer.getvalue())
    module_snapshot = source_control.snapshot(git_repo)
    for field in ("affected_paths", "dirty_files", "dirty_product",
                  "docs_only_dirty", "dirty", "entries"):
        assert cli_snapshot[field] == module_snapshot[field], field
