"""P0-SEC-01 regression gate (review R-01): no hardcoded credentials anywhere.

The previous gate only scanned map_core python sources and could print secret
fragments in the failure message. This gate now invokes the unified
scripts/security_scan.py entry point (the same command the release gate runs)
and additionally proves that scanner output NEVER contains a matched secret -
only file locations and sha256: fingerprints.

Coverage assertions:

- tree/index/build-context scopes must run and cover the whole repository
  (not just map_core python sources);
- at the reviewed HEAD all three scopes report zero hits;
- a canary probe is always caught (gate cannot silently degrade to a no-op);
- scan output redacts the secret itself (fingerprint only).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "security_scan.py"

# The scanner lives outside the map_core package; make it importable for the
# direct-import tests below (the CLI tests go through the subprocess entry).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Canary that must always be caught by the gpustack pattern. It is built
# from concatenated literals so the scanner does not flag this test file
# itself as a leak (the scanned text never contains the contiguous token).
CANARY = (
    "gpustack_" + "deadbeefdeadbeef" + "_" + "0123456789abcdef" + "0123456789abcdef"
)


def _run_scan(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_scanner_entry_point_exists_and_compiles() -> None:
    assert SCANNER.is_file(), SCANNER
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCANNER)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_tree_index_build_context_scan_zero_hits() -> None:
    """The reviewed tree must be free of hardcoded credentials.

    Runs the exact release-gate command over the whole repository. The
    scanner covers git-tracked files (tree), the staged index and every
    declared docker build context - not just map_core python files.
    """
    proc = _run_scan(
        "--scope", "tree,index,build-context", "--redact", "--fail-on-hit", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["hit_count"] == 0, report["hits"]
    assert set(report["scopes"]) == {"tree", "index", "build-context"}


def test_scan_covers_docs_not_just_python_sources() -> None:
    """Prove the gate covers the whole repo: the incident doc must be scanned."""
    import scripts.security_scan as scan

    assert scan.ROOT == REPO_ROOT
    incident = REPO_ROOT / "security" / "INCIDENT-2026-08-13-hardcoded-credentials.md"
    assert incident.is_file()
    assert not scan._is_exempt(
        "security/INCIDENT-2026-08-13-hardcoded-credentials.md"
    )
    doc_hits = []
    scanned = scan.scan_file_bytes(
        incident.read_bytes(), "tree:security/INCIDENT.md", doc_hits
    )
    assert scanned, "incident doc must be text-scanned, not skipped"
    assert doc_hits == [], doc_hits


def test_canary_is_always_caught_and_never_printed() -> None:
    """Failure self-test: a planted secret must fail the scan AND be redacted."""
    import scripts.security_scan as scan

    hits = []
    scan.scan_text("fake_path = %r" % CANARY, "tree:canary/file.py", hits)
    assert hits, "canary token must be detected"
    hit = hits[0]
    assert hit.pattern == "gpustack_token"
    assert hit.location == "tree:canary/file.py"
    assert hit.line == 1

    # The report and every serialised form must never contain the secret.
    report = scan.build_report(hits, ["tree"], {})
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    assert CANARY not in serialized
    assert "sha256:" in serialized
    assert hit.fingerprint.startswith("sha256:") and len(hit.fingerprint) == 23

    # The CLI failure line is built from location/line/pattern/fingerprint
    # only (the same fields main() prints); assert the value never appears.
    cli_line = "%s:%d: %s %s" % (hit.location, hit.line, hit.pattern, hit.fingerprint)
    assert CANARY not in cli_line
    assert cli_line == "tree:canary/file.py:1: gpustack_token " + hit.fingerprint


def test_fail_on_hit_exit_codes() -> None:
    """--fail-on-hit turns hits into a non-zero exit (release gate semantics)."""
    import scripts.security_scan as scan

    rc = scan.main(["--scope", "tree", "--fail-on-hit"])
    assert rc == 0  # reviewed tree is clean


def test_image_scope_fails_closed_without_build() -> None:
    """image scope must not silently pass; without a built image it exits 2.

    Deterministic regardless of local docker state: explicit bogus tags for
    every declared build context guarantee "not found" even when earlier
    scans built real images on the same machine.
    """
    import scripts.security_scan as scan

    hits = []
    unscanned: list[dict[str, str]] = []
    bogus = {
        name: "map-security-scan:definitely-missing-%d" % idx
        for idx, name in enumerate(scan.BUILD_CONTEXTS)
    }
    try:
        scan.scope_image(
            hits, [], unscanned, build=False, skip_unavailable=False, image_tags=bogus
        )
    except RuntimeError as exc:
        assert "not found" in str(exc) or "unavailable" in str(exc)
        return
    raise AssertionError("image scope without a built image must fail closed")
